# Stage 11 metrics and inference contract

This RED phase adds `study.metrics` and `study.bootstrap` as importable interfaces
with deliberate NotImplementedError bodies. The tests translate that exception to
an explanatory assertion failure. Existing study tests and implementation stay
unchanged. GREEN freezes these tests and the public signatures.

## Scores, bins, and decomposition

Inputs are nonempty aligned sequences: finite probabilities in [0,1], binary
outcomes, with no silent removal of invalid rows. Invalid inputs raise ValueError.
Brier is mean((p-y)^2), with no factor of two. Log loss uses natural logarithms and
clips p to [eps,1-eps], default eps=1e-4. Report the number of rows actually changed
by clipping, not the number lying exactly at a clipping bound. Require 0<eps<.5.

Reliability bins default to ten equal-width intervals. Membership is
min(floor(n_bins*p), n_bins-1), so decimal boundaries such as .1 and .3 go in the
bin to their right, and 1 belongs to the last bin. Keep empty bins, with n=0 and
mean_p/observed=None. Each populated bin reports the row-weighted mean forecast
and outcome frequency. n_bins is a positive integer.

Quantile bins use linear empirical quantiles at j/n_bins, j=0..n_bins, collapse
repeated edges, and never split tied forecasts. Intervals include their lower
edge, exclude the upper edge, and include the final upper edge. Constant forecasts
produce one degenerate interval [p,p]. Bin indices are consecutive and zero-based.

Murphy terms are computed using nonempty bin weights n_k/N:

- reliability = sum(weight * (mean_p - observed)^2)
- resolution = sum(weight * (observed - overall_y)^2)
- uncertainty = overall_y * (1-overall_y)

Their signed sum equals the original Brier score for forecasts constant within
bins. Do not force this identity for forecasts varying within a bin. The tests
include both situations, unequal outcomes, and randomized constant-within-bin data.

## Logistic calibration

Fit the unpenalized binary logistic model logit(P(Y=1)) = intercept + slope*logit(p),
with the same eps clipping as log loss. Report clipping count. Use the plan's
IRLS approach, with bounded iterations and finite positive tolerance. A converged
fit has finite coefficients and no reason. The two-level fixtures have exact
closed-form solutions; seeded Bernoulli simulations recover known parameters
within .08 at n=50,000 (deliberately broader than sampling error).

One outcome class (`one_class`), constant predictor (`constant_predictor`), and
complete/quasi separation (`separation`) have no identifiable finite solution.
Return converged=False with coefficients None. Use `non_convergence` if iteration
limits or numerical failure prevent a valid fit. Never publish a final partial
iterate as converged. The iteration-limit test allows an exact solution reached
within the budget; it does not mandate a particular initialization.

## Whole-event bootstrap

`cluster_bootstrap` computes one selected statistic: brier, log_loss, mean(y-p)
(`gap`), logistic intercept, or logistic slope. Defaults use 2000 replicates and
95% percentile intervals. Event IDs must be nonempty strings aligned with rows.
Require at least two resamples, confidence strictly between 0 and 1, positive
integer batch_size, and nonnegative integer seed.

For G distinct events, sort IDs lexicographically and draw G events with
replacement for each replicate. The reproducibility protocol is NumPy
`default_rng(seed).multinomial(G, [1/G]*G, size=n_resamples)`. Frequency weights
multiply ALL original rows of an event. Thus varying event sizes produce varying
replicate row counts. The estimand remains the row-weighted mean, not the unweighted
mean of event means. Brier and other additive metrics use sufficient statistics;
logistic replicates use batched IRLS, chunked to bound memory as required by the
build plan. Batching must preserve the RNG stream and answers, within floating
point tolerance. No process pool is needed for a single group; parallel groups
belong to later orchestration.

The point estimate uses original rows. Return all replicate values in draw order,
plus n_events and structured warning strings. Percentile endpoints use linear
order-statistic interpolation at (1-confidence)/2 and (1+confidence)/2.
An estimable single-event input has a degenerate interval and `single_event` warning.
This warning is carried in the result, not required as a Python runtime warning.

Undefined replicate statistics are None, never NaN or zero. If ANY replicate is
undefined, withhold both interval endpoints and include `invalid_replicates`.
This conservative rule avoids silently reporting an interval conditional on a
successful fit or nonempty bin. If the original statistic is undefined, its
estimate and interval are None and `undefined_estimate` is reported as well.
Keep valid replicates available for diagnostics even when the interval is withheld.

The unequal-event toy case has only three possible answers, 0, .75, and 1.
The logistic bootstrap fixture has two predictor levels with interior success
fractions under every resample; log-odds differences give exact per-replicate
coefficients independently of a numerical fitter. Expected draws come from the
specified random law, and expected statistics from this arithmetic, never from
production functions.

## Favorite-longshot gaps

Fit bin edges ONCE on the original sample. Use the same event draws across every
bin, resampling all G original events even when a particular bin contains fewer
events. Estimate observed-minus-price, i.e. mean(y-p). Every output holds the
original ReliabilityBin plus its BootstrapCI. Entirely empty bins have null
estimate/endpoints, None replicates and `empty_bin`. A replicate with no selected
rows in a bin is undefined and follows the interval-withholding rule above.
Quantile edges must not be re-estimated within a replicate.

## Coverage gate and validation

The slow test uses 200 fixed, independent simulations. Each has 100 independent
events, five identical outcomes per event, Y~Bernoulli(.25), p=.25. The population
Brier score is .25*.75=.1875. There are 999 bootstrap draws per simulation; the
required empirical 95% interval coverage is [.90,.99]. This is an implementation
check under one known data-generating process, not a coverage claim for real data.
An independent analytic calculation gives 186/200 for the fixed seeds: each draw's
score is 1/16 + (1/2)*resampled event outcome frequency. No production implementation
was used to select expected values or check this fixture.

Run the two new test modules to check RED, including the explicitly marked slow
test. Run existing tests separately to confirm they remain green. Quality checks
must stay clean while the new production functions remain deliberate stubs.

Background references:
- https://scikit-learn.org/stable/modules/model_evaluation.html (Brier scoring and Murphy terms)
- https://stat.ethz.ch/R-manual/R-devel/library/boot/html/boot.html (frequency-weight bootstrap representation)
