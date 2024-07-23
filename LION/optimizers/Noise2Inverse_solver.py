# numerical imports
from pathlib import Path
from scipy import optimize
import torch
import numpy as np
from torch.utils.data import DataLoader
from ts_algorithms import fdk

# Import base class
from LION.optimizers.LIONsolver import LIONsolver

# Parameter class
from LION.utils.parameter import LIONParameter

# standard imports
from tqdm import tqdm
import warnings

# CT imports
import LION.CTtools.ct_utils as ct

#### CAUTION: Only K->1 implemented
#### Implement 1->K and other modes
class Noise2Inverse_solver(LIONsolver):
    def __init__(
        self, model, optimizer, loss_fn, optimizer_params=None, verbose=True, geo=None
    ):
        super().__init__(model, optimizer, loss_fn, geo, solver_params=optimizer_params, device=optimizer_params.device)
        if geo is None:
            raise ValueError("Geometry must be given to Noise2Inverse_solver")
        self.sino_splits = optimizer_params.sino_splits
        self.recon_fn = optimizer_params.base_algo
        self.sub_op = self.make_sub_operators(self.geo, self.sino_splits)

    @staticmethod
    def default_parameters():
        param = LIONParameter()
        param.model = None
        param.optimizer = None
        param.loss_fn = None
        param.device = torch.cuda.current_device()
        param.validation_loader = None
        param.validation_fn = None
        param.validation_freq = 10
        param.save_folder = None
        param.checkpoint_freq = 10
        param.final_result_fname = None
        param.checkpoint_fname = None
        param.validation_fname = None
        param.epoch = None
        param.base_algo = fdk
        param.sino_splits = 4
        return param

    def set_validation(
        self,
        validation_loader: DataLoader,
        validation_freq: int,
        validation_fn: callable = None,
        validation_fname: Path = None,
    ):
        warnings.warn("Noise2Inverse_solver does not support validation")

    @staticmethod
    def make_sub_operators(geo, sino_splits):
        op = []
        angles = geo.angles.copy()
        for i in range(sino_splits):
            geo.angles = angles[i:-1:sino_splits]
            op.append(ct.make_operator(geo))
        return op

    def compute_noisy_sub_recon(self, data, target):
        # reserve memory
        size_noise2inv = list(target.shape)
        size_noise2inv.insert(0, self.sino_splits)
        bad_recon = torch.zeros(size_noise2inv, device=self.device)
        # compute noisy reconstructions
        for sino in range(data.shape[0]):
            for split in range(self.sino_splits):
                bad_recon[split, sino] = self.recon_fn(
                    self.sub_op[split], data[sino, 0, split : -1 : self.sino_splits]
                )

        return bad_recon

    def mini_batch_step(self, data, target):
        """
        This function isresponsible for performing a single mini-batch step of the optimization.
        returns the loss of the mini-batch
        """
        # Compute noisy reconstructions of subsets
        bad_recon = self.compute_noisy_sub_recon(data, target)

        # we train K->1 so, pick one of these to be the target, at random.
        label_array = torch.zeros(target.shape, device=self.device)
        random_target = torch.zeros(target.shape, device=self.device)
        for sino in range(data.shape[0]):
            indices = np.arange(self.sino_splits)
            label = np.random.randint(self.sino_splits)
            random_target[sino] = bad_recon[label, sino].detach().clone()
            label_array[sino] = torch.mean(
                bad_recon[np.delete(indices, label), sino].detach().clone(), axis=0
            )

        # set model to train (probs not needed, but just in case)
        self.model.train()
        # Zero gradients
        self.optimizer.zero_grad()
        # Forward pass
        output = self.model(label_array)
        # Compute loss
        self.loss = self.loss_fn(output, random_target)
        # Update optimizer and model
        self.loss.backward()
        self.optimizer.step()
        return self.loss.item()

    def train_step(self):
        """
        This function is responsible for performing a single tranining set epoch of the optimization.
        returns the average loss of the epoch
        """
        self.model.train()
        epoch_loss = 0.0
        for index, (data, target) in enumerate(self.train_loader):
            epoch_loss += self.mini_batch_step(data, target)
        return epoch_loss / len(self.train_loader)

    # No validation in Noise2Inverse (is this right?)
    def validate(self):
        return 0

    def epoch_step(self, epoch):
        """
        This function is responsible for performing a single epoch of the optimization.
        """
        self.train_loss[epoch] = self.train_step()

    def train(self, n_epochs):
        """
        This function is responsible for performing the optimization.
        """
        assert n_epochs > 0, "Number of epochs must be a positive integer"
        # Make sure all parameters are set
        self.check_complete()

        self.current_epoch = n_epochs
        self.train_loss = np.zeros(self.current_epoch)

        # train loop
        for epoch in tqdm(range(self.current_epoch)):
            self.epoch_step(epoch)
            if (epoch + 1) % self.checkpoint_freq == 0:
                self.save_checkpoint(epoch)

    def test(self):
        self.model.eval()
        print(f"Testing model after {self.current_epoch} epochs training")
        # do we want to be able to use this on a trained model? Surely yes?

        with torch.no_grad():
            test_loss = np.zeros(len(self.test_loader))
            for i, (sinos, targets) in enumerate(tqdm(self.test_loader)):
                bad_recon = self.compute_noisy_sub_recon(sinos, targets) # b, split, c, w, h
                
                
                # we train K->1 so, pick one of these to be the target, at random.
                label_array = torch.zeros(targets.shape, device=self.device)
                random_target = torch.zeros(targets.shape, device=self.device)
                for sino in range(sinos.shape[0]):
                    indices = np.arange(self.sino_splits)
                    label = np.random.randint(self.sino_splits)
                    random_target[sino] = bad_recon[label, sino].detach().clone()
                    label_array[sino] = torch.mean(
                        bad_recon[np.delete(indices, label), sino].detach().clone(), axis=0
                    )
                    # Forward pass
                output = self.model(label_array)
                # Compute loss
                test_loss[i] = self.loss_fn(output, random_target)
                # Update optimizer and model

        if self.verbose:
            print(
                f"Testing loss: {test_loss.mean()} - Testing loss std: {test_loss.std()}"
            )
        return test_loss

