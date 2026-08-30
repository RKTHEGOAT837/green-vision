"""The numeric model bake-off, and the hybrid that deploys its winner.

Candidates scored on the same held-out task (predict a cell's AQI/NDVI 12
months out, history strictly before the cutoff, skill measured against a
Theil-Sen + seasonality baseline; positive = beat the baseline).

MEASURED IN THIS REPOSITORY, on the shipped 146x42 Ahmedabad panel
(1056 train / 396 test samples, split by time at cutoff 26):

    statistical forecaster + memory  (training/backtest.py, 60 records)  +0.043
    trained RandomForest, 200 trees  (this package, `train --model rf`)  -0.500
    trained MLP 64x32                (this package, `train`, default)    -1.408

Reproduce with the commands at the bottom of this docstring; the numbers are
re-read from models/{city}/forecaster/report.json and models/{city}/
memory.meta.json, not transcribed.

NOT independently reproduced here, and so not claimed: an in-context score
for the Qwen2.5-1.5B INT4 LLM, and a ridge-on-residuals diagnostic. Both
appeared in an earlier draft of this docstring with figures (-0.33 and -0.03)
that no artefact in this repository backs. If you want them in a submission,
run them and write down what you get.

The verdict is the point, and it is negative: with 42 months of history,
city-wide shocks dominate the residual and neither trained challenger finds
signal the robust baseline misses. RandomForest loses by less than the MLP,
which is what you would expect from a smoother that cannot extrapolate a
trend — but losing by less is still losing. So the statistical forecaster
stays deployed for NUMBERS and the local LLM is used only for WORDS, and
`train` re-scores the challenger on every run: if one trained on a longer
panel ever reports positive held-out skill, the evidence to deploy it is
already in report.json. The challenger harness (ONNX export, OpenVINO
inference on CPU/GPU/NPU) is production-ready and waiting for more data.

Export fidelity is verified, not assumed: every exported graph is re-run
through the OpenVINO runtime against sklearn's own predictions on the
held-out set. Worst deviation measured 1.43e-06 (MLP) and 2.10e-07 (forest)
against a 1e-3 tolerance.

Train / re-score the challenger (writes models/{city}/forecaster/):
    python -m greenplan.forecast.train --config config/city.yaml
    python -m greenplan.forecast.train --config config/city.yaml --model rf
    python -m greenplan.forecast.train --config config/city.yaml --model rf --intel

`--intel` patches sklearn to dispatch to Intel oneDAL before any estimator is
built, and the report records whether the patch was active next to the
measured train time — running with and without the flag IS the acceleration
measurement. On this machine the forest fit went 1.5s -> 0.7s (~2.1x) with
combined skill unchanged (-0.500 -> -0.505), so the speedup costs no accuracy.
MLPRegressor is not on sklearnex's accelerated list, so `--intel` is a
documented no-op for the default model.
"""

from .features import FEATURE_NAMES, feature_vector
from .ovmodel import HybridModel, OVForecaster

__all__ = ["FEATURE_NAMES", "feature_vector", "OVForecaster", "HybridModel"]
