# Training the "jian yang" wake word

openWakeWord trains custom wake words from **synthetic** speech — you don't
record yourself saying the phrase over and over. A TTS model generates many
variations of "jian yang" (different voices/accents/speeds), those get
mixed with room-impulse-response and background-noise augmentation, and a
small classifier is trained to distinguish "jian yang" from everything else
(negative data: openWakeWord ships a large pre-built negative dataset of
generic speech/noise so you don't have to source that yourself).

This needs real compute (ideally a GPU) and takes a while — run it on a
laptop/desktop or Google Colab, **not** the Pi. The Pi only needs the final
exported `.onnx` file for inference, which is cheap.

## Steps

1. On a machine with more compute than the Pi, follow openWakeWord's
   automatic training notebook:
   https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb
   (Colab link is in that repo's README — "Training New Models"). Open it in
   Colab, or run it locally if you have the `openwakeword[train]` extras and
   `piper-sample-generator` installed.
2. Set the target phrase to `jian yang` in the notebook's config. Leave the
   rest of the pipeline (negative data, augmentation, training epochs) at
   the notebook's defaults for a first pass — tune later if the false-accept
   or false-reject rate in real use is bad.
3. Export the trained model in **ONNX** format (the notebook has an export
   cell for this — openWakeWord also supports tflite, but this repo's
   `voice/listen.py` loads with `inference_framework="onnx"`).
4. Copy the exported file to this Pi as:
   `voice/wakeword/jian_yang.onnx`
   (or point `WAKE_WORD_MODEL_PATH` in `.env` at wherever you put it).
5. Sanity-check it standalone before wiring up the full listener:
   ```
   voice/venv/bin/python -c "
   from openwakeword.model import Model
   import numpy as np
   m = Model(wakeword_models=['voice/wakeword/jian_yang.onnx'], inference_framework='onnx')
   print(m.predict(np.zeros(1280, dtype=np.int16)))
   "
   ```
   This should print a dict with one key (the model's name) and a score near
   0 for silence, without erroring — confirms the file loads and the key
   name `voice/listen.py` reads (`next(iter(oww.models.keys()))`) is what
   you expect.

## Calibration

`WAKE_WORD_THRESHOLD` in `.env` (default `0.5`) trades off false accepts
(triggers on other speech) against false rejects (you say it and nothing
happens). Watch `voice-listener`'s logs (`sudo journalctl -u
voice-listener -f`) for the score it logs on every trigger and near-trigger,
and adjust from there — this is very room/mic-dependent, expect to tune it
after the fact rather than getting it right up front.
