#!/usr/bin/env python3
"""Fine-tune the pretrained sjvasquez/handwriting-synthesis model on lines built by build_data.py.

Run from inside the (TF2-ported) sjvasquez checkout, with tfenv.sh sourced:

  python finetune.py --data ~/hw-train/v1data --ckpt ~/hw-train/ft1 --steps 3000 --lr 2e-5

The pretrained checkpoint (checkpoints/model-17900) is copied to --ckpt and training continues from step 17900, so the
step counts below are on top of that. The best checkpoint by validation loss is kept; the model files stay in --ckpt,
which is private (somebody's handwriting). Nothing here is for git.
"""
import argparse
import os
import pathlib
import shutil
import sys

import numpy as np

sys.path.insert(0, os.getcwd())
from data_frame import DataFrame  # noqa: E402
from rnn import DataReader, rnn  # noqa: E402

START = 17900


class Reader(DataReader):
    def __init__(self, data_dir):
        cols = ["x", "x_len", "c", "c_len"]
        d = pathlib.Path(data_dir)
        self.train_df = DataFrame(columns=cols, data=[np.load(d / "train" / f"{k}.npy") for k in cols])
        self.val_df = DataFrame(columns=cols, data=[np.load(d / "val" / f"{k}.npy") for k in cols])
        self.test_df = self.val_df
        print("train size", len(self.train_df), "val size", len(self.val_df))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--ckpt", required=True, help="private directory for the fine-tuned checkpoints")
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--patience", type=int, default=1500)
    ap.add_argument("--log", default="logs_ft")
    a = ap.parse_args()

    ck = pathlib.Path(a.ckpt).expanduser()
    ck.mkdir(parents=True, exist_ok=True, mode=0o700)
    for f in pathlib.Path("checkpoints").glob(f"model-{START}.*"):
        shutil.copy(f, ck / f.name)
    (ck / "checkpoint").write_text(f'model_checkpoint_path: "model-{START}"\nall_model_checkpoint_paths: "model-{START}"\n')

    nn = rnn(
        reader=Reader(os.path.expanduser(a.data)),
        log_dir=a.log,
        checkpoint_dir=str(ck),
        prediction_dir=str(ck / "predictions"),
        learning_rates=[a.lr],
        batch_sizes=[a.batch],
        patiences=[a.patience],
        beta1_decays=[.9],
        validation_batch_size=32,
        optimizer="rms",
        num_training_steps=START + a.steps,
        warm_start_init_step=START,
        regularization_constant=0.0,
        keep_prob=1.0,
        enable_parameter_averaging=False,
        min_steps_to_checkpoint=START + 100,
        log_interval=20,
        grad_clip=10,
        lstm_size=400,
        output_mixture_components=20,
        attention_mixture_components=10,
    )
    nn.fit()
