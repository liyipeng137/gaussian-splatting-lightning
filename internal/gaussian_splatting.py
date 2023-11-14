import os.path
from typing import Tuple, List, Union

import torch.optim
import torchvision
import wandb
from lightning.pytorch.core.module import MODULE_OPTIMIZERS
from torchmetrics.image import PeakSignalNoiseRatio
from lightning.pytorch import LightningDataModule, LightningModule
from lightning.pytorch.utilities.types import OptimizerLRScheduler, LRSchedulerPLType
import lightning.pytorch.loggers

from internal.configs.model import ModelParams
# from internal.configs.appearance import AppearanceModelParams

from internal.models.gaussian_model import GaussianModel
# from internal.models.appearance_model import AppearanceModel
from internal.renderers import Renderer, VanillaRenderer
from internal.utils.ssim import ssim
from jsonargparse import lazy_instance


class GaussianSplatting(LightningModule):
    def __init__(
            self,
            gaussian: ModelParams,
            save_iterations: List[int],
            camera_extent_factor: float = 1.,
            # enable_appearance_model: bool = False,
            background_color: Tuple[float, float, float] = (0., 0., 0.),
            output_path: str = None,
            save_val_output: bool = False,
            max_save_val_output: int = -1,
            renderer: Renderer = lazy_instance(VanillaRenderer),
    ) -> None:
        super().__init__()
        self.automatic_optimization = False
        self.save_hyperparameters()

        # setup models
        self.gaussian_model = GaussianModel(sh_degree=gaussian.sh_degree)
        # self.appearance_model = None if enable_appearance_model is False else AppearanceModel(
        #     n_input_dims=1,
        #     n_grayscale_factors=appearance.n_grayscale_factors,
        #     n_gammas=appearance.n_gammas,
        #     n_neurons=appearance.n_neurons,
        #     n_hidden_layers=appearance.n_hidden_layers,
        #     n_frequencies=appearance.n_frequencies,
        #     grayscale_factors_activation=appearance.grayscale_factors_activation,
        #     gamma_activation=appearance.gamma_activation,
        # )

        self.optimization_hparams = self.hparams["gaussian"].optimization

        self.renderer = renderer

        # metrics
        self.lambda_dssim = gaussian.optimization.lambda_dssim
        self.psnr = PeakSignalNoiseRatio()

        self.background_color = torch.tensor(background_color, dtype=torch.float32)

        self.batch_size = 1

    def _l1_loss(self, predict: torch.Tensor, gt: torch.Tensor):
        return torch.abs(predict - gt).mean()

    def _l2_loss(self, predict: torch.Tensor, gt: torch.Tensor):
        return torch.mean((predict - gt) ** 2)

    def setup(self, stage: str):
        if stage == "fit":
            self.cameras_extent = self.trainer.datamodule.dataparser_outputs.camera_extent
            self.prune_extent = self.trainer.datamodule.prune_extent
            self.gaussian_model.create_from_pcd(
                self.trainer.datamodule.point_cloud,
                spatial_lr_scale=self.cameras_extent,
            )
            self.gaussian_model.training_setup(self.hparams["gaussian"].optimization)

            # scale after create_from_pcd(), avoid lr scaling
            self.cameras_extent *= self.hparams["camera_extent_factor"]
            self.prune_extent *= self.hparams["camera_extent_factor"]

        self.renderer.setup(stage, lightning_module=self)

        self.log_image = None
        if isinstance(self.logger, lightning.pytorch.loggers.TensorBoardLogger):
            self.log_image = self.tensorboard_log_image
        elif isinstance(self.logger, lightning.pytorch.loggers.WandbLogger):
            self.log_image = self.wandb_log_image

        # set loss function
        self.rgb_diff_loss_fn = self._l1_loss
        if self.hparams["gaussian"].optimization.rgb_diff_loss == "l2":
            print("Use L2 loss")
            self.rgb_diff_loss_fn = self._l2_loss

    def tensorboard_log_image(self, tag: str, image_tensor):
        self.logger.experiment.add_image(
            tag,
            image_tensor,
            self.trainer.global_step,
        )

    def wandb_log_image(self, tag: str, image_tensor):
        image_dict = {
            tag: wandb.Image(image_tensor),
        }
        self.logger.experiment.log(
            image_dict,
            step=self.trainer.global_step,
        )

    def forward(self, camera):
        return self.renderer(
            camera,
            self.gaussian_model,
            # self.appearance_model,
            bg_color=self.background_color.to(camera.R.device)
        )

    def forward_with_loss_calculation(self, camera, image_info):
        image_name, gt_image, masked_pixels = image_info

        # forward
        outputs = self(camera)

        image = outputs["render"]

        # calculate loss
        if masked_pixels is not None:
            gt_image[masked_pixels] = image.detach()[masked_pixels]  # copy masked pixels from prediction to G.T.
        rgb_diff_loss = self.rgb_diff_loss_fn(outputs["render"], gt_image)
        ssim_metric = ssim(outputs["render"], gt_image)
        loss = (1.0 - self.lambda_dssim) * rgb_diff_loss + self.lambda_dssim * (1. - ssim_metric)

        return outputs, loss, rgb_diff_loss, ssim_metric

    def optimizers(self, use_pl_optimizer: bool = True):
        optimizers = super().optimizers(use_pl_optimizer=use_pl_optimizer)

        if isinstance(optimizers, list) is False:
            return [optimizers]

        """
        IMPORTANCE: the global_step will be increased on every step() call of all the optimizers,
        issue https://github.com/Lightning-AI/lightning/issues/17958,
        here change _on_before_step and _on_after_step to override this behavior.
        """
        for idx, optimizer in enumerate(optimizers):
            if idx == 0:
                continue
            optimizer._on_before_step = lambda: self.trainer.profiler.start("optimizer_step")
            optimizer._on_after_step = lambda: self.trainer.profiler.stop("optimizer_step")

        return optimizers

    def lr_schedulers(self) -> Union[None, List[LRSchedulerPLType], LRSchedulerPLType]:
        schedulers = super().lr_schedulers()

        if schedulers is None:
            return []

        if isinstance(schedulers, list) is False:
            return [schedulers]

        return schedulers

    def training_step(self, batch, batch_idx):
        camera, image_info = batch

        gt_image = image_info[1]
        ht = gt_image.shape[1]
        wt = gt_image.shape[2]
        camera.width = wt
        camera.height = ht
        # image_name, gt_image, masked_pixels = image_info

        global_step = self.trainer.global_step + 1  # must start from 1 to prevent densify at the beginning

        # get optimizers and schedulers
        optimizers = self.optimizers()

        schedulers = self.lr_schedulers()

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if global_step % 1000 == 0:
            self.gaussian_model.oneupSHdegree()

        # forward
        outputs, loss, rgb_diff_loss, ssim_metric = self.forward_with_loss_calculation(camera, image_info)
        image, viewspace_point_tensor, visibility_filter, radii = outputs["render"], outputs["viewspace_points"], \
            outputs["visibility_filter"], outputs["radii"]

        # calculate loss
        # if masked_pixels is not None:
        #     gt_image[masked_pixels] = image.detach()[masked_pixels]  # copy masked pixels from prediction to G.T.
        # l1_loss = torch.abs(outputs["render"] - gt_image).mean()
        # ssim_metric = ssim(outputs["render"], gt_image)
        # loss = (1.0 - self.lambda_dssim) * l1_loss + self.lambda_dssim * (1. - ssim_metric)

        self.log("train/rgb_diff", rgb_diff_loss, on_step=True, on_epoch=False, prog_bar=False, batch_size=self.batch_size)
        self.log("train/ssim", ssim_metric, on_step=True, on_epoch=False, prog_bar=False, batch_size=self.batch_size)
        self.log("train/loss", loss, on_step=True, on_epoch=False, prog_bar=True, batch_size=self.batch_size)

        # log learning rate
        if (global_step - 1) % 100 == 0:
            metrics_to_log = {}
            for opt_idx, opt in enumerate(optimizers):
                if opt is None:
                    continue
                for idx, param_group in enumerate(opt.param_groups):
                    param_group_name = param_group["name"] if "name" in param_group else str(idx)
                    metrics_to_log["lr/{}_{}".format(opt_idx, param_group_name)] = param_group["lr"]
            self.logger.log_metrics(
                metrics_to_log,
                step=global_step - 1,
            )

        # backward
        for optimizer in optimizers:
            optimizer.zero_grad(set_to_none=True)
        self.manual_backward(loss)

        # save before densification
        if global_step in self.hparams["save_iterations"]:
            # TODO: save on training end
            self.save_gaussian_to_ply()

        # before gradient descend
        with torch.no_grad():
            # Densification
            if global_step < self.hparams["gaussian"].optimization.densify_until_iter:
                gaussians = self.gaussian_model
                gaussians.max_radii2D[visibility_filter] = torch.max(
                    gaussians.max_radii2D[visibility_filter],
                    radii[visibility_filter]
                )
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if global_step > self.optimization_hparams.densify_from_iter and global_step % self.optimization_hparams.densification_interval == 0:
                    size_threshold = 20 if global_step > self.optimization_hparams.opacity_reset_interval else None
                    gaussians.densify_and_prune(
                        self.hparams["gaussian"].optimization.densify_grad_threshold,
                        0.005,
                        extent=self.cameras_extent,
                        prune_extent=self.prune_extent,
                        max_screen_size=size_threshold,
                    )

                if global_step % self.hparams["gaussian"].optimization.opacity_reset_interval == 0 or \
                        (
                                torch.all(self.background_color == 1.) and global_step == self.hparams[
                            "gaussian"].optimization.densify_from_iter
                        ):
                    gaussians.reset_opacity()

        # optimize
        for optimizer in optimizers:
            optimizer.step()

        # schedule lr
        self.gaussian_model.update_learning_rate(global_step)
        for scheduler in schedulers:
            scheduler.step()

    def validation_step(self, batch, batch_idx):
        camera, image_info = batch
        gt_image = image_info[1]
        ht = gt_image.shape[1]
        wt = gt_image.shape[2]
        camera.width = wt
        camera.height = ht

        # forward
        outputs, loss, rgb_diff_loss, ssim_metric = self.forward_with_loss_calculation(camera, image_info)

        # calculate loss
        # l1_loss = torch.abs(outputs["render"] - gt_image).mean()
        # ssim_metric = ssim(outputs["render"], gt_image)
        # loss = (1.0 - self.lambda_dssim) * l1_loss + self.lambda_dssim * (1. - ssim_metric)

        self.log("val/rgb_diff", rgb_diff_loss, on_epoch=True, prog_bar=False, batch_size=self.batch_size)
        self.log("val/ssim", ssim_metric, on_epoch=True, prog_bar=False, batch_size=self.batch_size)
        self.log("val/loss", loss, on_epoch=True, prog_bar=True, batch_size=self.batch_size)
        self.log("val/psnr", self.psnr(outputs["render"], gt_image), on_epoch=True, prog_bar=True,
                 batch_size=self.batch_size)

        if self.trainer.global_rank == 0 and self.hparams["save_val_output"] is True and (
                self.hparams["max_save_val_output"] < 0 or batch_idx < self.hparams["max_save_val_output"]
        ):
            if self.log_image is not None:
                grid = torchvision.utils.make_grid(torch.concat([outputs["render"], gt_image], dim=-1))
                self.log_image(
                    tag="val_images/{}".format(image_info[0].replace("/", "_")),
                    image_tensor=grid,
                )

            image_output_path = os.path.join(
                self.hparams["output_path"],
                "val",
                "epoch_{}".format(self.trainer.current_epoch),
                "{}.png".format(image_info[0].replace("/", "_"))
            )
            os.makedirs(os.path.dirname(image_output_path), exist_ok=True)
            torchvision.utils.save_image(
                torch.concat([outputs["render"], gt_image], dim=-1),
                image_output_path,
            )

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def configure_optimizers(self):
        optimizers = [
            self.gaussian_model.optimizer,
        ]
        schedulers = []

        # appearance model
        # if self.hparams["enable_appearance_model"] is True:
        #     appearance_optimizer = torch.optim.Adam(
        #         [
        #             {"params": list(self.appearance_model.parameters()), "name": "appearance"}
        #         ],
        #         lr=self.hparams["appearance"].optimization.lr,
        #         eps=self.hparams["appearance"].optimization.eps,
        #     )
        #     appearance_scheduler = torch.optim.lr_scheduler.LambdaLR(
        #         optimizer=appearance_optimizer,
        #         lr_lambda=lambda iter: self.hparams["appearance"].optimization.gamma ** min(iter / self.hparams["appearance"].optimization.max_steps, 1),
        #         verbose=False,
        #     )
        #
        #     optimizers.append(appearance_optimizer)
        #     schedulers.append(appearance_scheduler)

        # renderer
        renderer_optimizer, renderer_scheduler = self.renderer.training_setup()
        if renderer_optimizer is not None:
            optimizers.append(renderer_optimizer)
        if renderer_scheduler is not None:
            schedulers.append(renderer_scheduler)

        return optimizers, schedulers

    def save_gaussian_to_ply(self):
        filename = "point_cloud.ply"
        if self.trainer.global_rank != 0:
            filename = "point_cloud_{}.ply".format(self.trainer.global_rank)
        with torch.no_grad():
            output_dir = os.path.join(self.hparams["output_path"], "point_cloud",
                                      "iteration_{}".format(self.trainer.global_step))
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, filename)
            self.gaussian_model.save_ply(output_path + ".tmp")
            os.rename(output_path + ".tmp", output_path)

        print("Gaussians saved to {}".format(output_path))

        checkpoint_path = os.path.join(
            self.hparams["output_path"],
            "checkpoints",
            "epoch={}-step={}.ckpt".format(self.trainer.current_epoch, self.trainer.global_step),
        )
        os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
        self.trainer.save_checkpoint(checkpoint_path)
        print("checkpoint save to {}".format(checkpoint_path))
