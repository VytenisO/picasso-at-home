# train_v1: fine-tune sjvasquez/handwriting-synthesis on a writer's samples

Result on the first dataset (202 prompts): readable but not good enough for plotting; archived. Code kept as a reference.
Everything private (samples, checkpoints) lives outside git, under `handwriting/writers/<name>/`.

## Setup on the GPU machine (done on robotlab, RTX 4090)
1. `git clone https://github.com/sjvasquez/handwriting-synthesis v1` (no licence; do not commit it here).
2. `python3 -m venv envtf && envtf/bin/pip install 'tensorflow[and-cuda]' tf-keras matplotlib pandas scikit-learn scipy svgwrite`
3. `tfenv.sh`: put every `site-packages/nvidia/*/lib` on `LD_LIBRARY_PATH`, set `TF_USE_LEGACY_KERAS=1`.
4. Port the repo to TF2 through `tf.compat.v1` (the pinned TF 1.6 cannot use a current GPU):
   - every `import tensorflow as tf` becomes `import tensorflow.compat.v1 as tf` plus `tf.disable_v2_behavior()`
   - `tf.contrib.rnn.LSTMCell` becomes `tf.nn.rnn_cell.LSTMCell`; `tf.contrib.layers.variance_scaling_initializer()`
     becomes `tf.variance_scaling_initializer(scale=2.0, mode='fan_in', distribution='truncated_normal')`
   - `rnn_cell.py`: replace the `tfd` (tf.contrib.distributions) sampling by explicit normal / uniform / categorical draws;
     `np.ones_like(es)` becomes `tf.ones_like(es)`
   - `rnn_ops.py`: local `_like_rnncell`, `context.in_graph_mode()` becomes `not context.executing_eagerly()`,
     `control_flow_ops.cond/while_loop` become `tf.cond/tf.while_loop`
   - `tf_base_model.py`: `list(self.metrics.values())`, `sum(int(np.prod(...)) ...)`; `demo.py`: `.tostring()` becomes `.tobytes()`
   - `rnn.py`: the samplers also return `tf.argmax(states.phi, -1)` (which character is attended), exposed as `nn.sampled_attn`
   The pretrained checkpoint (step 17900) loads unchanged after this.

## Run
```
python build_data.py --data DATA --writer NAME --out V1DATA --repo ~/hw-train/v1 --points-per-char 34.5
cd ~/hw-train/v1 && . ../tfenv.sh
python finetune.py --data V1DATA --ckpt CKPT --steps 3000 --lr 2e-5      # early stopping kept the best step
python generate.py --ckpt CKPT --samples DATA/writers/NAME/samples --out OUT "some text"
```
`generate.py` also levels the baseline (`--straighten`, `--curve`) and adds i/j dots and t crosses in the writer's own
shape, taken from their isolated-letter samples, because the model rarely crosses a t.

## Limits
Alphabet is fixed by the pretrained weights: a-z, A-Z without Q X Z, digits, space and `! " # ' ( ) , - . : ; ?`;
no æ ø å. Lines are at most 75 characters.
