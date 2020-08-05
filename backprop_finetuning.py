import gc
import matplotlib.pyplot as plt
import numpy as np
import os
import pandas as pd
import torch
import torch.nn as nn
import torchvision

# To view tensorboard metrics
# tensorboard --logdir=logs --port=6006 --bind_all
from torch.utils.tensorboard import SummaryWriter
from functools import partial
from evolver import CrossoverType, MutationType, MatrixEvolver
from unet import UNet
from dataset_utils import PartitionType
from cuda_utils import maybe_get_cuda_device, clear_cuda
from landcover_dataloader import get_landcover_dataloaders

from ignite.contrib.handlers.tensorboard_logger import *
from ignite.engine import Events, create_supervised_trainer, create_supervised_evaluator
from ignite.metrics import Accuracy, Loss, ConfusionMatrix, mIoU
from ignite.handlers import ModelCheckpoint
from ignite.utils import setup_logger

# Define directories for data, logging and model saving.
base_dir = os.getcwd()
dataset_name = "landcover_large"
dataset_dir = os.path.join(base_dir, "data/" + dataset_name)

experiment_name = "backprop_finetuning"
model_name = "best_model_30_validation_accuracy=0.9409.pt"
model_path = os.path.join(base_dir, "logs/" + dataset_name + "/" + model_name)
log_dir = os.path.join(base_dir, "logs/" + dataset_name + "_" + experiment_name)

# Create DataLoaders for each partition of Landcover data.
dataloader_params = {
    'batch_size': 16,
    'shuffle': True,
    'num_workers': 6,
    'pin_memory': True}

partition_types = [PartitionType.TRAIN, PartitionType.VALIDATION, 
                   PartitionType.FINETUNING, PartitionType.TEST]
data_loaders = get_landcover_dataloaders(dataset_dir, 
                                         partition_types,
                                         dataloader_params,
                                         force_create_dataset=False)

finetuning_loader = data_loaders[2]
test_loader = data_loaders[3]

# Get GPU device if available.
device = maybe_get_cuda_device()

# Determine model and training params.
params = {
    'max_epochs': 10,
    'n_classes': 4,
    'in_channels': 4,
    'depth': 5,
    'learning_rate': 0.01,
    'log_steps': 1,
    'save_top_n_models': 4
}

clear_cuda()    
model = UNet(in_channels = params['in_channels'],
             n_classes = params['n_classes'],
             depth = params['depth'])
model.load_state_dict(torch.load(model_path))

model.to(device)
criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), 
                             lr=params['learning_rate'])


# Determine metrics for evaluation.
train_metrics = {
        "accuracy": Accuracy(), 
        "loss": Loss(criterion),
        "mean_iou": mIoU(ConfusionMatrix(num_classes = params['n_classes'])),
        }

validation_metrics = {
        "accuracy": Accuracy(), 
        "loss": Loss(criterion),
        "mean_iou": mIoU(ConfusionMatrix(num_classes = params['n_classes'])),

}

# Create Trainer or Evaluators
trainer = create_supervised_trainer(model, optimizer, criterion, device=device)
train_evaluator = create_supervised_evaluator(model, metrics=train_metrics, device=device)
validation_evaluator = create_supervised_evaluator(model, metrics=validation_metrics, device=device)

trainer.logger = setup_logger("Trainer")
train_evaluator.logger = setup_logger("Train Evaluator")
validation_evaluator.logger = setup_logger("Validation Evaluator")

# Tensorboard Logger setup below based on pytorch ignite example
# https://github.com/pytorch/ignite/blob/master/examples/contrib/mnist/mnist_with_tensorboard_logger.py
@trainer.on(Events.EPOCH_COMPLETED)
def compute_metrics(engine):
    """Callback to compute metrics on the train and validation data."""
    train_evaluator.run(finetuning_loader)
    validation_evaluator.run(test_loader)

def score_function(engine):
    """Function to determine the metric upon which to compare model."""
    return engine.state.metrics["accuracy"]
    
# Setup Tensor Board Logging    
tb_logger = TensorboardLogger(log_dir=log_dir)

tb_logger.attach_output_handler(
    trainer,
    event_name=Events.ITERATION_COMPLETED(every=params['log_steps']),
    tag="training",
    output_transform=lambda loss: {"batchloss": loss},
    metric_names="all",
)

for tag, evaluator in [("training", train_evaluator), ("validation", validation_evaluator)]:
    tb_logger.attach_output_handler(
        evaluator,
        event_name=Events.EPOCH_COMPLETED,
        tag=tag,
        metric_names="all",
        global_step_transform=global_step_from_engine(trainer),
    )

tb_logger.attach_opt_params_handler(trainer, 
                                    event_name=Events.ITERATION_COMPLETED(every=params['log_steps']), 
                                    optimizer=optimizer)

model_checkpoint = ModelCheckpoint(
    log_dir,
    n_saved=params['save_top_n_models'],
    filename_prefix="best",
    score_function=score_function,
    score_name="validation_accuracy",
    global_step_transform=global_step_from_engine(trainer),
)

validation_evaluator.add_event_handler(Events.COMPLETED, model_checkpoint, {"model": model})
trainer.run(finetuning_loader, max_epochs=params['max_epochs'])
tb_logger.close()
