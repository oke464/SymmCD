import math, copy
import json, os
import numpy as np
import pandas as pd
from p_tqdm import p_map, t_map
import itertools

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch_geometric.data import DataLoader

from collections import defaultdict, Counter
from typing import Any, Dict, List

import hydra
import pytorch_lightning as pl
from tqdm import tqdm

from pyxtal.symmetry import search_cloest_wp, Group

from symmcd.common.utils import PROJECT_ROOT
from symmcd.common.data_utils import (
    lattice_params_to_matrix_torch, lattice_ks_to_matrix_torch, sg_to_ks_mask, mask_ks,)

from symmcd.pl_modules.diff_utils import d_log_p_wrapped_normal
from symmcd.pl_modules.model import build_mlp


MAX_ATOMIC_NUM=94
SITE_SYMM_AXES = 15
SITE_SYMM_PGS = 13
SITE_SYMM_DIM = SITE_SYMM_AXES * SITE_SYMM_PGS
SG_CONDITION_DIM = 397
SG_SYM = {spacegroup: Group(spacegroup) for spacegroup in range(1, 231)}
SG_TO_WP_TO_SITE_SYMM = dict()
for spacegroup in range(1, 231):
    SG_TO_WP_TO_SITE_SYMM[spacegroup] = dict()
    for wp in SG_SYM[spacegroup].Wyckoff_positions:
        wp.get_site_symmetry()
        SG_TO_WP_TO_SITE_SYMM[spacegroup][wp] = wp.get_site_symmetry_object().to_one_hot()

from scripts.generation import SampleDataset
from scripts.compute_metrics import Crystal, GenEval, get_gt_crys_ori
from scripts.eval_utils import lattices_to_params_shape, smact_validity, structure_validity, get_crystals_list
import re


def find_num_atoms(dummy_ind, total_num_atoms):
    # num_atoms states how many atoms are there in each crystal (num_repr + dummy origin)
    actual_num_atoms = []
    atoms = 0
    for num in total_num_atoms:
        # find number of 0 in dummy_ind from atoms to atoms+num
        actual_num_atoms.append(torch.sum(dummy_ind[atoms:atoms+num] == 0).item())
        atoms += num
        
    return torch.tensor(actual_num_atoms)

def split_argmax_sitesymm(site_symm:torch.Tensor) -> np.ndarray:
    '''
    Return argmax for each axis
    '''
    return site_symm.reshape(-1, SITE_SYMM_AXES, SITE_SYMM_PGS).argmax(dim=-1)



def modify_frac_coords_one(frac_coords, site_symm, atom_types, spacegroup):
    spacegroup = spacegroup.item()
    site_symm_axis = site_symm.reshape(-1, SITE_SYMM_AXES, SITE_SYMM_PGS).detach().cpu()
    # Get site symmetry of each WP for the spacegroup
    wp_to_site_symm = SG_TO_WP_TO_SITE_SYMM[spacegroup]

    # iterate over frac coords and corresponding site-symm
    new_frac_coords, new_atom_types, new_site_symm = [], [], []
    for (sym, frac_coord, atm_type) in zip(site_symm_axis, frac_coords, atom_types):
        frac_coord = frac_coord.cpu().detach().numpy()
        
        # Get all WPs that are closest in terms of site symmetry
        wp_to_ss_dist = {wp: torch.norm(sym.flatten() - ss.flatten()) for wp, ss in wp_to_site_symm.items()}
        min_ss_dist = min(wp_to_ss_dist.values())
        closest_ss_wps = [wp for wp, dist in wp_to_ss_dist.items() if dist==min_ss_dist]
            
        # For each WP find closest position in space
        closes = []
        for wp in closest_ss_wps:
            for orbit_index in range(len(wp.ops)):
                close = search_cloest_wp(SG_SYM[spacegroup], wp, wp.ops[orbit_index], frac_coord)%1.
                closes.append((close, wp, orbit_index, np.linalg.norm(np.minimum((close - frac_coord)%1., (frac_coord - close)%1.))))
        try:
            # pick the nearest wp to project
            closest = sorted(closes, key=lambda x: x[-1])[0]
            wyckoff = closest[1]
            repr_index = closest[2]
            
            # use wp operations on frac_coord
            frac_coord = closest[0]
            for index in range(len(wyckoff)): 
                new_frac_coords.append(wyckoff[(index + repr_index) % len(wyckoff)].operate(frac_coord)%1.)
                new_atom_types.append(atm_type.cpu().detach().numpy())
                new_site_symm.append(sym)
        except:
            new_frac_coords.append(frac_coord)
            new_atom_types.append(atm_type.cpu().detach().numpy())
            new_site_symm.append(sym.cpu().detach().numpy())
            
    new_frac_coords = np.stack(new_frac_coords)
    new_atom_types = np.stack(new_atom_types)
    new_site_symm = np.stack(new_site_symm)
    return new_frac_coords, len(new_frac_coords), new_atom_types, new_site_symm

def modify_frac_coords(traj:Dict, spacegroups:List[int], num_repr:List[int]) -> Dict:
    device = traj['frac_coords'].device
    total_atoms = 0
    updated_frac_coords = []
    updated_num_atoms = []
    updated_atom_types = []
    updated_site_symm = []
    print("Replicating atoms based on site symmetries")
    for index in tqdm(range(len(num_repr))):
        if num_repr[index] > 0:
            new_frac_coords, new_num_atoms, new_atom_types, new_site_sym = modify_frac_coords_one(
                    traj['frac_coords'][total_atoms:total_atoms+num_repr[index]],
                    traj['site_symm'][total_atoms:total_atoms+num_repr[index]], 
                    traj['atom_types'][total_atoms:total_atoms+num_repr[index]], 
                    spacegroups[index], 
                )
            if new_num_atoms:
                updated_frac_coords.append(new_frac_coords)
                updated_num_atoms.append(new_num_atoms)
                updated_atom_types.append(new_atom_types)
                updated_site_symm.append(new_site_sym)
        
        total_atoms += num_repr[index]
    
    traj['frac_coords'] = torch.cat([torch.from_numpy(x) for x in updated_frac_coords]).to(device)
    traj['atom_types'] = torch.cat([torch.from_numpy(x) for x in updated_atom_types]).to(device)
    traj['num_atoms'] = torch.tensor(updated_num_atoms).to(device)
    traj['site_symm'] = torch.cat([torch.from_numpy(x) for x in updated_site_symm]).to(device)
    
    return traj

class BaseModule(pl.LightningModule):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__()
        # populate self.hparams with args and kwargs automagically!
        self.save_hyperparameters()

    def configure_optimizers(self):
        opt = hydra.utils.instantiate(
            self.hparams.optim.optimizer, params=self.parameters(), _convert_="partial"
        )
        if not self.hparams.optim.use_lr_scheduler:
            return [opt]
        scheduler = hydra.utils.instantiate(
            self.hparams.optim.lr_scheduler, optimizer=opt
        )
        lr_scheduler_config = {
            "scheduler": scheduler,
            "interval": "epoch",
            "frequency": 1,
            "monitor": "val_loss",
            "strict": True,
            "name": None,
        }
        return {"optimizer": opt, "lr_scheduler": lr_scheduler_config}


### Model definition

class SinusoidalTimeEmbeddings(nn.Module):
    """ Attention is all you need. """
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, time):
        device = time.device
        half_dim = self.dim // 2
        embeddings = math.log(10000) / (half_dim - 1)
        embeddings = torch.exp(torch.arange(half_dim, device=device) * -embeddings)
        embeddings = time[:, None] * embeddings[None, :]
        embeddings = torch.cat((embeddings.sin(), embeddings.cos()), dim=-1)
        return embeddings


class CSPDiffusion(BaseModule):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.decoder = hydra.utils.instantiate(self.hparams.decoder, latent_dim = self.hparams.latent_dim + self.hparams.time_dim, pred_type = True, pred_site_symm_type = True, smooth = True, max_atoms=MAX_ATOMIC_NUM)
        self.beta_scheduler = hydra.utils.instantiate(self.hparams.beta_scheduler)
        self.sigma_scheduler = hydra.utils.instantiate(self.hparams.sigma_scheduler)
        self.time_dim = self.hparams.time_dim
        self.time_embedding = SinusoidalTimeEmbeddings(self.time_dim)
        self.spacegroup_embedding = build_mlp(in_dim=SG_CONDITION_DIM, hidden_dim=128, fc_num_layers=2, out_dim=self.time_dim)
        self.keep_lattice = self.hparams.cost_lattice < 1e-5
        self.keep_coords = self.hparams.cost_coord < 1e-5
        self.use_ks = self.hparams.use_ks
        self.mask_ss = self.hparams.mask_ss
        if self.mask_ss:
            self.group_ss_mask = self.init_group_ss_mask()

    def init_group_ss_mask(self):
        sg_to_group_ss_mask = np.zeros((231, SITE_SYMM_AXES, SITE_SYMM_PGS))
        for spacegroup_number in range(1, 231):
            group = Group(spacegroup_number)
            group_mask = None
            for wp in group.Wyckoff_positions:
                wp.get_site_symmetry()
                site_symm_binary = wp.get_site_symmetry_object().to_one_hot()
                if group_mask is None:
                    group_mask = site_symm_binary
                else:
                    group_mask = group_mask + site_symm_binary
            sg_to_group_ss_mask[spacegroup_number] = group_mask != 0
        return torch.FloatTensor(sg_to_group_ss_mask)


    def forward(self, batch):

        batch_size = batch.num_graphs
        
        times = self.beta_scheduler.uniform_sample_t(batch_size, self.device)
        time_emb = self.time_embedding(times) + self.spacegroup_embedding(batch.sg_condition.reshape(-1, SG_CONDITION_DIM))

        if self.mask_ss:
            batch_ss_mask = self.group_ss_mask[torch.repeat_interleave(batch.spacegroup, batch.num_atoms)].flatten(1, 2).to(self.device)
        # get diffusion coefficients for site symmetry, lattice, and atom types diffusion
        alphas_cumprod = self.beta_scheduler.alphas_cumprod[times]
        c0 = torch.sqrt(alphas_cumprod)
        c1 = torch.sqrt(1. - alphas_cumprod)
        c0_lattice = c0[:, self.beta_scheduler.LATTICE]
        c1_lattice = c1[:, self.beta_scheduler.LATTICE]
        c0_atom = c0[:, self.beta_scheduler.ATOM]
        c1_atom = c1[:, self.beta_scheduler.ATOM]
        c0_site_symm = c0[:, self.beta_scheduler.SITE_SYMM]
        c1_site_symm = c1[:, self.beta_scheduler.SITE_SYMM]

        # get coefficients for coordinate diffusion
        sigmas = self.sigma_scheduler.sigmas[times]
        sigmas_norm = self.sigma_scheduler.sigmas_norm[times]
        
        ks = batch.ks
        if self.use_ks:
            lattices = lattice_ks_to_matrix_torch(batch.ks)
            ks_mask, ks_add = sg_to_ks_mask(batch.spacegroup)
        else:
            lattices = lattice_params_to_matrix_torch(batch.lengths, batch.angles)

        frac_coords = batch.frac_coords

        rand_x = torch.randn_like(frac_coords)
        rand_ks = torch.randn_like(ks)
        rand_l = torch.randn_like(lattices)

        if self.use_ks:
            input_ks = c0_lattice[:, None] * ks + c1_lattice[:, None] * rand_ks
            input_ks = mask_ks(input_ks, ks_mask, ks_add)
            input_lattice = lattice_ks_to_matrix_torch(input_ks)
        else:
            input_lattice = c0_lattice[:, None, None] * lattices + c1_lattice[:, None, None] * rand_l
            
            
        sigmas_per_atom = sigmas.repeat_interleave(batch.num_atoms)[:, None]
        sigmas_norm_per_atom = sigmas_norm.repeat_interleave(batch.num_atoms)[:, None]
        input_frac_coords = (frac_coords + sigmas_per_atom * rand_x) % 1.

        gt_atom_types_onehot = F.one_hot(batch.atom_types - 1, num_classes=MAX_ATOMIC_NUM).float()
        gt_site_symm_binary = batch.site_symm.flatten(1, 2)

        rand_t = torch.randn_like(gt_atom_types_onehot)
        rand_symm = torch.randn_like(gt_site_symm_binary)

        atom_type_probs = (c0_atom.repeat_interleave(batch.num_atoms)[:, None] * gt_atom_types_onehot + c1_atom.repeat_interleave(batch.num_atoms)[:, None] * rand_t)
        site_symm_probs = (c0_site_symm.repeat_interleave(batch.num_atoms)[:, None] * gt_site_symm_binary + c1_site_symm.repeat_interleave(batch.num_atoms)[:, None] * rand_symm)
        if self.mask_ss:
            site_symm_probs = batch_ss_mask * site_symm_probs

        if self.keep_coords:
            input_frac_coords = frac_coords

        if self.keep_lattice:
            input_lattice = lattices
            input_ks = ks

        # pass noised site symmetries and behave similar to atom type probs
        lattice_feats = input_ks if self.use_ks else input_lattice
        preds = self.decoder(time_emb, atom_type_probs, input_frac_coords, 
                            lattice_feats, input_lattice, batch.num_atoms, 
                            batch.batch, site_symm_probs=site_symm_probs)
        pred_lattice, pred_x, pred_t, pred_symm  = preds

        tar_x = d_log_p_wrapped_normal(sigmas_per_atom * rand_x, sigmas_per_atom) / torch.sqrt(sigmas_norm_per_atom)

        
        loss_lattice = F.mse_loss(pred_lattice, ks_mask * rand_ks) if self.use_ks else F.mse_loss(pred_lattice, rand_l)

        loss_coord = torch.mean(torch.sqrt(batch.x_loss_coeff) * F.mse_loss(pred_x, tar_x, reduction='none'))
        
        loss_type = F.mse_loss(pred_t, rand_t)
            
        if self.mask_ss:
            loss_symm = torch.mean(F.mse_loss(batch_ss_mask * pred_symm, batch_ss_mask * rand_symm, reduction='none'))
        else:
            loss_symm = torch.mean(F.mse_loss(pred_symm, rand_symm, reduction='none'))

        loss = (
            self.hparams.cost_lattice * loss_lattice +
            self.hparams.cost_coord * loss_coord + 
            self.hparams.cost_type * loss_type +
            self.hparams.cost_symm * loss_symm
        )

        return {
            'loss' : loss,
            'loss_lattice' : loss_lattice,
            'loss_coord' : loss_coord,
            'loss_type' : loss_type,
            'loss_symm' : loss_symm,
        }

    @torch.no_grad()
    def sample(self, batch, diff_ratio = 1.0, step_lr = 1e-5):


        batch_size = batch.num_graphs

        if self.use_ks:
            ks_mask, ks_add = sg_to_ks_mask(batch.spacegroup)
            k_T = torch.randn([batch_size, 6]).to(self.device)
            k_T = mask_ks(k_T, ks_mask, ks_add)
            l_T = lattice_ks_to_matrix_torch(k_T)
        else:
            l_T = torch.randn([batch_size, 3, 3]).to(self.device)
            k_T = torch.zeros([batch_size, 6]).to(self.device) # not used
        x_T = torch.rand([batch.num_nodes, 3]).to(self.device)
        t_T = torch.randn([batch.num_nodes, MAX_ATOMIC_NUM]).to(self.device)
        
        symm_T = torch.randn([batch.num_nodes, SITE_SYMM_DIM]).to(self.device)
        if self.mask_ss:
            batch_ss_mask = self.group_ss_mask[torch.repeat_interleave(batch.spacegroup, batch.num_atoms)].flatten(1, 2).to(self.device)
            symm_T = batch_ss_mask * symm_T
        # site_symm_mask = sg_to_wyckoff_mask(batch.spacegroup.repeat_interleave(batch.num_atoms)).to(self.device)
        # symm_T = site_symm_mask * symm_T

        if self.keep_coords:
            x_T = batch.frac_coords

        if self.keep_lattice:
            k_T = batch.ks
            l_T = lattice_ks_to_matrix_torch(k_T) if self.use_ks else lattice_params_to_matrix_torch(batch.lengths, batch.angles)

        traj = {self.beta_scheduler.timesteps : {
            'num_atoms' : batch.num_atoms,
            'atom_types' : t_T,
            'site_symm' : symm_T,
            'frac_coords' : x_T % 1.,
            'lattices' : l_T,
            'ks' : k_T,
            'spacegroup': batch.spacegroup,
        }}

        for t in tqdm(range(self.beta_scheduler.timesteps, 0, -1)):

            times = torch.full((batch_size, ), t, device = self.device)

            # get diffusion timestep embeddings, concatenated with spacegroup condition 
            time_emb = self.time_embedding(times) + self.spacegroup_embedding(batch.sg_condition.reshape(-1, SG_CONDITION_DIM))
            
            alphas = self.beta_scheduler.alphas[t]
            alphas_cumprod = self.beta_scheduler.alphas_cumprod[t]

            sigmas = self.beta_scheduler.sigmas[t]
            sigma_x = self.sigma_scheduler.sigmas[t]
            sigma_norm = self.sigma_scheduler.sigmas_norm[t]

            c0 = 1.0 / torch.sqrt(alphas)
            c1 = (1 - alphas) / torch.sqrt(1 - alphas_cumprod)
            
            c0_lattice = c0[self.beta_scheduler.LATTICE]
            c1_lattice = c1[self.beta_scheduler.LATTICE]
            c0_atom = c0[self.beta_scheduler.ATOM]
            c1_atom = c1[self.beta_scheduler.ATOM]
            c0_site_symm = c0[self.beta_scheduler.SITE_SYMM]
            c1_site_symm = c1[self.beta_scheduler.SITE_SYMM]

            x_t = traj[t]['frac_coords']
            l_t = traj[t]['lattices']
            t_t = traj[t]['atom_types']
            symm_t = traj[t]['site_symm']
            k_t = traj[t]['ks']


            if self.keep_coords:
                x_t = x_T

            if self.keep_lattice:
                l_t = l_T
                k_t = k_T

            # Corrector
            if self.use_ks:
                rand_k = torch.randn_like(k_T) if t > 1 else torch.zeros_like(k_T)
            else:
                rand_l = torch.randn_like(l_T) if t > 1 else torch.zeros_like(l_T)
            rand_t = torch.randn_like(t_T) if t > 1 else torch.zeros_like(t_T)
            rand_symm = torch.randn_like(symm_T) if t > 1 else torch.zeros_like(symm_T)
            if self.mask_ss:
                rand_symm = batch_ss_mask * rand_symm
            rand_x = torch.randn_like(x_T) if t > 1 else torch.zeros_like(x_T)

            step_size = step_lr * (sigma_x / self.sigma_scheduler.sigma_begin) ** 2
            std_x = torch.sqrt(2 * step_size)

            lattice_feats_t = k_t if self.use_ks else l_t
            preds = self.decoder(time_emb, t_t, x_t, 
                            lattice_feats_t, l_t, batch.num_atoms, 
                            batch.batch, site_symm_probs=symm_t)
            
            _, pred_x, _, _  = preds

            pred_x = pred_x * torch.sqrt(sigma_norm)

            x_t_minus_05 = x_t - step_size * pred_x + std_x * rand_x if not self.keep_coords else x_t

            l_t_minus_05 = l_t
            k_t_minus_05 = k_t

            t_t_minus_05 = t_t

            symm_t_minus_05 = symm_t


            # Predictor
            if self.use_ks:
                rand_k = torch.randn_like(k_T) if t > 1 else torch.zeros_like(k_T)
            else:
                rand_l = torch.randn_like(l_T) if t > 1 else torch.zeros_like(l_T)

            rand_t = torch.randn_like(t_T) if t > 1 else torch.zeros_like(t_T)
            rand_symm = torch.randn_like(symm_T) if t > 1 else torch.zeros_like(symm_T)
            if self.mask_ss:
                rand_symm = batch_ss_mask * rand_symm
            rand_x = torch.randn_like(x_T) if t > 1 else torch.zeros_like(x_T)

            adjacent_sigma_x = self.sigma_scheduler.sigmas[t-1] 
            step_size = (sigma_x ** 2 - adjacent_sigma_x ** 2)
            std_x = torch.sqrt((adjacent_sigma_x ** 2 * (sigma_x ** 2 - adjacent_sigma_x ** 2)) / (sigma_x ** 2))   
            lattice_feats_t_minus_05 = k_t_minus_05 if self.use_ks else l_t_minus_05
            
            preds = self.decoder(time_emb, t_t_minus_05, x_t_minus_05, 
                            lattice_feats_t_minus_05, l_t_minus_05, batch.num_atoms, 
                            batch.batch, site_symm_probs=symm_t_minus_05)
            
            pred_l, pred_x, pred_t, pred_symm  = preds

            pred_x = pred_x * torch.sqrt(sigma_norm)

            x_t_minus_1 = x_t_minus_05 - step_size * pred_x + std_x * rand_x if not self.keep_coords else x_t

            if self.use_ks:
                k_t_minus_1 = c0_lattice * (k_t_minus_05 - c1_lattice * pred_l) + sigmas[self.beta_scheduler.LATTICE] * rand_k if not self.keep_lattice else k_t
                k_t_minus_1 = mask_ks(k_t_minus_1, ks_mask, ks_add)
                l_t_minus_1 = lattice_ks_to_matrix_torch(k_t_minus_1) if not self.keep_lattice else l_t
            else:
                l_t_minus_1 = c0_lattice * (l_t_minus_05 - c1_lattice * pred_l) + sigmas[self.beta_scheduler.LATTICE] * rand_l if not self.keep_lattice else l_t
                k_t_minus_1 = k_t

            t_t_minus_1 = c0_atom * (t_t_minus_05 - c1_atom * pred_t) + sigmas[self.beta_scheduler.ATOM] * rand_t

            symm_t_minus_1 = c0_site_symm * (symm_t_minus_05 - c1_site_symm * pred_symm) + sigmas[self.beta_scheduler.SITE_SYMM] * rand_symm
            if self.mask_ss:
                symm_t_minus_1 = batch_ss_mask * symm_t_minus_1

            traj[t - 1] = {
                'num_atoms' : batch.num_atoms,
                'atom_types' : t_t_minus_1,
                'site_symm' : symm_t_minus_1,
                'frac_coords' : x_t_minus_1 % 1.,
                'lattices' : l_t_minus_1,
                'ks' : k_t_minus_1,
                'spacegroup' : batch.spacegroup,
            }

        traj_stack = {
            'num_atoms' : batch.num_atoms,
            'atom_types' : torch.stack([traj[i]['atom_types'] for i in range(self.beta_scheduler.timesteps, -1, -1)]).argmax(dim=-1) + 1,
            'site_symm' : torch.stack([traj[i]['site_symm'] for i in range(self.beta_scheduler.timesteps, -1, -1)]),
            'all_frac_coords' : torch.stack([traj[i]['frac_coords'] for i in range(self.beta_scheduler.timesteps, -1, -1)]),
            'all_lattices' : torch.stack([traj[i]['lattices'] for i in range(self.beta_scheduler.timesteps, -1, -1)]),
            'all_ks': torch.stack([traj[i]['ks'] for i in range(self.beta_scheduler.timesteps, -1, -1)]),
            'all_spacegroup': torch.stack([traj[i]['spacegroup'] for i in range(self.beta_scheduler.timesteps, -1, -1)]),
        }


        # drop all dummy elements (atom types = MAX_ATOMIC_NUM)
        dummy_ind = (traj[0]['atom_types'].argmax(dim=-1) + 1 == MAX_ATOMIC_NUM).long()
        traj[0]['frac_coords'] = traj[0]['frac_coords'][(1 - dummy_ind).bool()]
        traj[0]['atom_types'] = traj[0]['atom_types'][(1 - dummy_ind).bool()]
        traj[0]['site_symm'] = traj[0]['site_symm'][(1 - dummy_ind).bool()]
        
        # find for each crystal how many non-dummy atoms are there
        traj[0]['num_atoms'] = find_num_atoms(dummy_ind, batch.num_atoms).to(self.device)
        
        # remove lattices and ks for empty crystals corresponding to num_atoms = 0
        empty_crystals = (traj[0]['num_atoms'] == 0).long()
        traj[0]['ks'] = traj[0]['ks'][(1 - empty_crystals).bool()]
        traj[0]['lattices'] = traj[0]['lattices'][(1 - empty_crystals).bool()]
        print(f"Number of empty crystals generated: {empty_crystals.sum().item()}/{batch_size}")
        # use predicted site symmetry to create copies of atoms
        traj[0] = modify_frac_coords(traj[0], batch.spacegroup, traj[0]['num_atoms'])
        
        # sanity checks for size of tensors
        assert traj[0]['frac_coords'].size(0) == traj[0]['atom_types'].size(0) == traj[0]['num_atoms'].sum(), breakpoint()
        assert traj[0]['ks'].size(0) == traj[0]['lattices'].size(0) == traj[0]['num_atoms'].size(0), breakpoint()

        return traj[0], traj_stack



    def training_step(self, batch: Any, batch_idx: int) -> torch.Tensor:

        output_dict = self(batch)

        loss_lattice = output_dict['loss_lattice']
        loss_coord = output_dict['loss_coord']
        loss_type = output_dict['loss_type']
        loss_symm = output_dict['loss_symm']
        loss = output_dict['loss']


        self.log_dict(
            {'train_loss': loss,
            'lattice_loss': loss_lattice,
            'coord_loss': loss_coord,
            'type_loss': loss_type,
            'symm_loss': loss_symm,
            },
            on_step=True,
            on_epoch=True,
            prog_bar=True,
        )

        if loss.isnan():
            return None

        return loss

    def validation_step(self, batch: Any, batch_idx: int) -> torch.Tensor:

        output_dict = self(batch)

        log_dict, loss = self.compute_stats(output_dict, prefix='val')

        self.log_dict(
            log_dict,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
        )
        
        if (self.current_epoch + 1) % self.hparams.data.eval_every_epoch == 0 and batch_idx == 0:
            # run a simpler evaluation
            self.simple_gen_evaluation()
    
        return loss

    def simple_gen_evaluation(self):
        
        eval_model_name_dataset = {
            "mp20": "mp", # encompasses mp20, mpts52
            "perovskite": "perov",
            "carbon": "carbon",
        }
        test_set = SampleDataset(
                            eval_model_name_dataset[self.hparams.data.eval_model_name], 
                            self.hparams.data.eval_generate_samples, 
                            self.hparams.data.datamodule.datasets.train.save_path)
        
        test_loader = DataLoader(test_set, batch_size = 50)
        
        frac_coords = []
        num_atoms = []
        atom_types = []
        lattices = []
        spacegroups = []
        site_symmetries = []
        for idx, batch in enumerate(test_loader):

            if torch.cuda.is_available():
                batch.cuda()
            outputs, traj = self.sample(batch, step_lr = 1e-5)
            del traj
            frac_coords.append(outputs['frac_coords'].detach().cpu())
            num_atoms.append(outputs['num_atoms'].detach().cpu())
            atom_types.append(outputs['atom_types'].detach().cpu())
            lattices.append(outputs['lattices'].detach().cpu())
            spacegroups.append(outputs['spacegroup'].detach().cpu())
            site_symmetries.append(outputs['site_symm'].detach().cpu())
            del outputs

        frac_coords = torch.cat(frac_coords, dim=0)
        num_atoms = torch.cat(num_atoms, dim=0)
        atom_types = torch.cat(atom_types, dim=0)
        lattices = torch.cat(lattices, dim=0)
        spacegroups = torch.cat(spacegroups, dim=0)
        site_symmetries = torch.cat(site_symmetries, dim=0)
        lengths, angles = lattices_to_params_shape(lattices)
        
        # generated crystals
        kwargs = {"spacegroups": spacegroups, "site_symmetries": site_symmetries}
        pred_crys_array_list = get_crystals_list(frac_coords, atom_types, lengths, angles, num_atoms, **kwargs)
        gen_crys = p_map(lambda x: Crystal(x), pred_crys_array_list)
        print(f"INFO: Done generating {self.hparams.data.eval_generate_samples} crystals (Epoch: {self.current_epoch + 1})")
        
        # ground truth crystals
        if os.path.exists(self.hparams.data.datamodule.datasets.val[0].gt_crys_path):
            gt_crys = torch.load(self.hparams.data.datamodule.datasets.val[0].gt_crys_path)
        else:
            csv = pd.read_csv(self.hparams.data.datamodule.datasets.val[0].path)
            gt_crys = t_map(get_gt_crys_ori, csv['cif'])
            torch.save(gt_crys, self.hparams.data.datamodule.datasets.val[0].gt_crys_path)
            
        print(f"INFO: Done reading ground truth crystals (Epoch: {self.current_epoch + 1})")
        
        gen_evaluator = GenEval(gen_crys, gt_crys, n_samples=0, eval_model_name=self.hparams.data.eval_model_name,
                                gt_prop_eval_path=self.hparams.data.datamodule.datasets.val[0].gt_prop_eval_path)
        gen_metrics = gen_evaluator.get_metrics()
        print(gen_metrics)
        
        self.log_dict(gen_metrics)
    
    def test_step(self, batch: Any, batch_idx: int) -> torch.Tensor:

        output_dict = self(batch)

        log_dict, loss = self.compute_stats(output_dict, prefix='test')

        self.log_dict(
            log_dict,
        )
        return loss

    def compute_stats(self, output_dict, prefix):

        loss_lattice = output_dict['loss_lattice']
        loss_coord = output_dict['loss_coord']
        loss_type = output_dict['loss_type']
        loss_symm = output_dict['loss_symm']
        loss = output_dict['loss']

        log_dict = {
            f'{prefix}_loss': loss,
            f'{prefix}_lattice_loss': loss_lattice,
            f'{prefix}_coord_loss': loss_coord,
            f'{prefix}_type_loss': loss_type,
            f'{prefix}_symm_loss': loss_symm,
        }

        return log_dict, loss

    