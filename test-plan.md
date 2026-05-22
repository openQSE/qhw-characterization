# IQM 20-Qubit Characterization Test Plan

## Goals

The goal is to build an evidence-backed model of the installed IQM
20-qubit system that can be used in two ways:

- operate the machine with a QFw backend
- parameterize a `qSchedSim` device with measured execution-time and
  noise behavior

The plan focuses on four questions:

- Given a circuit, can we predict its hardware execution time?
- Which gates execute in parallel, and where does parallel execution break?
- How does result quality degrade with width, depth, gate type, and layout?
- Which machine parameters should be exported into QFw and `qSchedSim`?

## Tooling Choice

Use Qiskit as the primary circuit authoring interface. Store generated
circuits and transpiled circuits as artifacts so that tests remain
reproducible.

Reasons:

- IQM provides a maintained Qiskit adapter through `iqm-client[qiskit]`.
- Qiskit exposes backend targets, coupling maps, transpilation output,
  circuit metadata, and standard analysis tooling.
- QFw already has a Qiskit-facing backend path, so this minimizes the
  translation layer needed for application tests.
- Raw QASM is useful as an interchange artifact, but it is not the best
  source format for tests because it loses higher-level intent, transpiler
  metadata, and backend-specific target information.

Use OpenQASM only as a saved artifact or for compatibility tests. If an IQM
native circuit representation exposes timing or calibration details that
Qiskit does not expose, add a lower-level IQM-client path for those specific
tests rather than making QASM the main interface.

References:

- IQM Client documentation: https://docs.meetiqm.com/iqm-client/
- Qiskit on IQM user guide:
  https://docs.meetiqm.com/iqm-client/user_guide_qiskit.html
- IQM Qiskit adapter API:
  https://docs.meetiqm.com/iqm-client/api/iqm.qiskit_iqm.html

## Required Data Products

Every test run should write a structured record. Use JSON Lines for raw
records and Parquet or CSV for processed tables.

Each record should include:

- timestamp
- machine identifier
- calibration set id
- backend target summary
- qubit list and physical qubit mapping
- gate counts before transpilation
- gate counts after transpilation
- circuit depth before transpilation
- circuit depth after transpilation
- scheduled depth, if available
- shots
- job id
- queue time, if available
- execution time, if available
- total wall time measured by the client
- result counts
- expected distribution or ideal state summary
- fidelity or distance metric
- raw circuit artifact path
- transpiled circuit artifact path
- software versions

If the IQM API or local control stack exposes lower-level timing telemetry,
capture that separately from client wall time. Client wall time is not enough
to infer hardware execution time because it includes submission, queueing,
server processing, and result transfer.

## Required Environment

All test scripts and the QFw IQM service should use the same environment
contract:

```bash
export QFW_QC_URL=<quantum-computer-url>
export QFW_API_KEY=<api-key>
```

`QFW_QC_URL` is the URL for the IQM quantum computer endpoint.
`QFW_API_KEY` is the API key used to authenticate with that endpoint.

Scripts should fail fast with a clear error if either variable is missing.
Do not hardcode machine URLs or API keys in test scripts, service code, or
configuration files. If a campaign needs to record which machine was used, it
should record the URL or a sanitized machine identifier in the output metadata,
not the API key.

## Phase 0: Machine Discovery

Start by collecting the static and calibration-dependent machine model.

Tests and artifacts:

- backend name, version, and API endpoint
- calibration set id and calibration timestamp
- number of active qubits
- qubit names and indices
- native gate set
- coupling graph
- supported two-qubit edges
- supported measurement operations
- maximum circuits per job
- maximum shots per circuit
- any scheduling, duration, or instruction timing information exposed by
  the backend target
- current readout error, gate error, T1, and T2 values if exposed by the
  calibration API

Deliverables:

- `device_snapshot.json`
- `coupling_graph.json`
- `calibration_snapshot.json`
- `qSchedSim` static device skeleton

## Phase 1: Execution-Time Characterization

Separate execution-time measurements into four components:

- fixed job overhead
- shot-dependent overhead
- circuit schedule duration
- readout and reset contribution

The target model should be:

```text
T_total_observed =
    T_submit_queue_result
  + T_job_fixed
  + shots * (T_reset + T_schedule(circuit) + T_measure)
```

For `qSchedSim`, the useful model is the hardware portion:

```text
T_hardware(circuit, shots) =
    T_job_fixed
  + shots * (T_reset + T_schedule(circuit) + T_measure)
```

If hardware telemetry cannot separate queue and execution time, fit both a
client-observed model and a hardware-informed model. Keep them separate.

### Fixed and Shot-Scaling Tests

Run the smallest legal circuits:

- empty or identity circuit with measurement
- one measured qubit
- all 20 qubits measured
- shots sweep: 1, 10, 100, 1000, 5000, 10000, subject to system limits
- batch-size sweep: 1, 2, 4, 8, 16 circuits per job, subject to limits

Questions:

- Is the fixed overhead per job or per circuit?
- Does execution time scale linearly with shots?
- Does measuring more qubits change runtime?
- Does batching amortize submission overhead?

Fit:

```text
T = alpha + beta * shots
T = alpha_job + alpha_circuit * num_circuits + beta * shots
```

### Single-Qubit Gate Duration Tests

For each qubit and each native one-qubit gate:

- build circuits with repeated gates on one qubit
- depths: 1, 2, 4, 8, 16, 32, 64, 128
- measure the target qubit
- use enough shots to reduce timing noise
- run each circuit multiple times over the day

Questions:

- What is the per-gate duration for each native one-qubit gate?
- Are durations uniform across qubits?
- Does execution time scale with depth after subtracting fixed overhead?

Fit:

```text
T_per_shot = intercept + depth * gate_duration
```

### Two-Qubit Gate Duration Tests

For each supported physical edge and each native two-qubit gate:

- repeat the two-qubit gate on the same edge
- depths: 1, 2, 4, 8, 16, 32, 64
- measure both qubits
- run multiple repetitions

Questions:

- What is the per-gate duration per edge?
- Are edge durations uniform?
- Are some edges slower or more error-prone?

Fit:

```text
T_per_shot = intercept + depth * edge_gate_duration
```

### Parallel Single-Qubit Gate Tests

Create layers of simultaneous one-qubit gates.

Examples:

- 1 active qubit, depth D
- 2 active qubits, depth D
- 4 active qubits, depth D
- 8 active qubits, depth D
- 16 active qubits, depth D
- 20 active qubits, depth D

Use the same depth and gate type across each width.

Questions:

- Does one layer of N one-qubit gates take the same time as one gate?
- Does runtime depend on the number of active qubits?
- Is there a control-system fanout limit?

Expected model if gates are parallel:

```text
T_layer ~= max(duration(gate_i))
```

Non-parallel behavior will appear as a width-dependent term:

```text
T_layer ~= max(duration(gate_i)) + gamma * active_qubits
```

### Parallel Two-Qubit Gate Tests

Use disjoint edges from the coupling graph.

Examples:

- one two-qubit gate on one edge
- two disjoint two-qubit gates in the same layer
- maximum matching of disjoint two-qubit gates
- repeated layers of a fixed matching
- alternating matchings that cover more of the chip

Questions:

- Can independent two-qubit gates execute in parallel?
- Does parallelism depend on physical edge placement?
- Are there frequency-collision or control-line conflicts?
- Does simultaneous execution increase error rates?

Fit both timing and quality:

```text
T_layer = max(edge_duration_i)
T_layer = sum(edge_duration_i)
T_layer = max(edge_duration_i) + congestion(edge_set)
```

The selected model should be chosen by residual error, not assumption.

### Measurement and Reset Tests

Measure:

- one qubit
- all qubits
- subsets of qubits
- repeated prepare-measure circuits
- circuits with explicit reset if supported

Questions:

- Is readout parallel across qubits?
- Does readout time depend on the number of measured qubits?
- Is reset implicit between shots?
- Is reset time constant or qubit dependent?

## Phase 2: Accuracy and Noise Characterization

The objective is to learn when a circuit stops producing useful output.
Use several metrics because no single metric covers all circuit families.

Metrics:

- heavy-output probability
- Hellinger fidelity
- total variation distance
- KL divergence where appropriate
- probability of expected bitstring
- parity expectation error
- cross-entropy benchmarking score
- success probability for structured algorithms

### Readout Characterization

Run assignment-error circuits:

- prepare `|0>` and measure each qubit
- prepare `|1>` and measure each qubit
- prepare selected computational basis states
- all-zero and all-one states
- random bitstrings across 20 qubits

Deliverables:

- per-qubit readout error
- pairwise readout correlation
- full assignment matrix for small subsets
- scalable tensor-product approximation for larger subsets
- readout mitigation configuration

### Coherence Tests

Run standard calibration-style circuits if they are not already provided by
the control stack.

T1:

- prepare `|1>`
- wait over a sweep of delay times
- measure decay to `|0>`

T2 Ramsey:

- prepare superposition
- wait over a sweep of delay times
- apply phase-sensitive closing pulse
- fit dephasing oscillation

T2 echo:

- prepare superposition
- wait, echo pulse, wait
- measure decay

Deliverables:

- T1 per qubit
- T2 Ramsey per qubit
- T2 echo per qubit
- uncertainty estimates
- drift over time

### Gate Fidelity Tests

Use randomized benchmarking where possible.

Tests:

- single-qubit randomized benchmarking per qubit
- simultaneous single-qubit RB across many qubits
- interleaved RB for important one-qubit gates
- two-qubit RB per supported edge
- simultaneous two-qubit RB on disjoint edges
- interleaved RB for native two-qubit gates

If full RB is too expensive, use lighter alternatives:

- mirror circuits
- cycle benchmarking
- direct fidelity estimation for selected states
- process tomography on selected one-qubit gates and a small number of
  two-qubit edges

Deliverables:

- one-qubit error per Clifford
- two-qubit error per Clifford or per native gate
- simultaneous-gate degradation factors
- per-edge fidelity map
- crosstalk matrix

### Depth-Limit Tests

Use circuit families with known ideal outcomes:

- GHZ chains from 2 to 20 qubits
- mirror circuits with variable depth
- random Clifford circuits
- QV-like square circuits
- layered hardware-efficient ansatz circuits
- QAOA-style circuits on hardware-native graphs
- one-dimensional and graph-local entangling circuits

Sweep:

- qubits: 1 through 20
- depth: 1, 2, 4, 8, 16, 32, 64, 128
- layout: best qubits, worst qubits, random connected subgraphs
- shots: enough to bound statistical error

Questions:

- At what depth does output become indistinguishable from noise?
- Does useful depth depend more on two-qubit count, total depth, or layout?
- How strongly do calibration metrics predict success?

Deliverables:

- useful-depth threshold by circuit family
- useful-depth threshold by qubit subset
- empirical quality model for `qSchedSim`

## Phase 3: Crosstalk and Parallelism Quality

Timing parallelism is not enough. A layer may execute in parallel but produce
lower-fidelity results.

Tests:

- single-qubit RB alone versus simultaneous on all qubits
- two-qubit RB alone versus simultaneous on disjoint edges
- spectator tests where idle neighboring qubits are prepared and measured
- readout crosstalk using simultaneous measurement patterns
- frequency-collision tests if the backend exposes relevant qubit metadata

Deliverables:

- crosstalk matrix
- parallel-gate penalty model
- qubit and edge exclusion rules for scheduling

## Phase 4: Drift and Stability

Repeat a small sentinel suite frequently.

Sentinel tests:

- all-zero readout
- all-one readout
- single-qubit RB on representative qubits
- two-qubit RB on representative edges
- GHZ on a fixed connected chain
- fixed-depth mirror circuits
- fixed timing probes

Run cadence:

- before and after each characterization campaign
- every hour during long campaigns
- after calibration changes
- after system maintenance

Deliverables:

- drift plots
- calibration-to-performance correlation
- recommended validity window for `qSchedSim` device parameters

## Phase 5: Execution-Time Formula for qSchedSim

Build the formula in layers.

First model:

```text
T(circuit, shots) =
    T_fixed
  + shots * (T_reset + T_measure(width) + sum(layer_duration(layer)))
```

Layer duration:

```text
layer_duration(layer) =
    max(duration(op, qubit_or_edge) for op in layer)
  + congestion_penalty(layer)
```

Congestion penalty starts as zero. Add terms only if measurements show that
parallel gates are not fully parallel.

Candidate congestion terms:

- number of active qubits in the layer
- number of active two-qubit edges
- number of gates sharing a control region
- edge-specific conflict penalties
- simultaneous readout width

The fitted `qSchedSim` device should include:

- qubit count
- coupling graph
- native gate set
- one-qubit duration per qubit and gate
- two-qubit duration per edge and gate
- measurement duration model
- reset duration model
- per-job fixed overhead
- per-shot overhead
- parallelism rule
- congestion penalty model
- readout error model
- one-qubit fidelity model
- two-qubit fidelity model
- coherence model
- drift validity window

## QFw Backend Plan

Add an IQM execution backend to QFw rather than making the tests call IQM
directly forever. The IQM integration should use the existing `api_qpm`
interface. It should be another selectable QPM service, not a parallel API
stack.

Initial backend:

- create a QFw QPM service for IQM execution
- reuse `service-apis/api_qpm`
- make the IQM service discoverable through the existing QPM reservation path
- read `QFW_QC_URL` and `QFW_API_KEY` from the service environment
- submit Qiskit circuits through the IQM Qiskit backend
- capture job ids and backend metadata
- return counts in the same shape as existing QFw backends
- include calibration set id and timing metadata in the result payload
- preserve the existing simulator QPM services without changing their public
  API

Recommended structure:

```text
QFw/
  services/
    svc_iqm_qpm/
      svc_qpm.py
      util_iqm.py
```

All IQM-specific execution logic should live in `svc_iqm_qpm`. The existing
QFw Qiskit-facing layer already provides the client-side abstraction needed
to select QPM services by type and capability. It should not grow a separate
IQM backend unless a future requirement appears that cannot be represented by
the existing QPM selection model.

Capability model changes:

- add an explicit IQM backend capability bit
- add a generic superconducting capability bit
- allow callers to request IQM specifically when they need this physical
  system
- allow callers to request any superconducting QPM when the workload only
  requires superconducting hardware
- keep existing simulator capability bits for TNQVM and NWQ-Sim

Example selection behavior:

- `QFW_CAP_IQM`: reserve only an IQM QPM service
- `QFW_CAP_SUPERCONDUCTING`: reserve any superconducting QPM service
- `QFW_CAP_STATEVECTOR`: reserve a statevector-capable simulator service
- `QFW_TYPE_NWQSIM` or equivalent existing selector: reserve NWQ-Sim

The IQM service should advertise both the explicit IQM capability and the
generic superconducting capability. If future superconducting backends are
added, they should advertise the generic capability and their own explicit
backend capability.

Backend requirements:

- fail fast if `QFW_QC_URL` or `QFW_API_KEY` is missing
- support circuit submission
- support batch submission
- expose backend target information
- expose calibration snapshot metadata when available
- support dry-run and validation-only modes
- support result retrieval by job id
- record raw IQM job metadata for traceability

Do not hide IQM calibration-set identity. The same physical machine with a
different calibration set should be treated as a different measured device
configuration.

## Test Script Layout

Suggested repository layout:

```text
IQM-TestScripts/
  test-plan.md
  pyproject.toml
  README.md
  configs/
    iqm_backend.yaml
    campaigns/
      timing.yaml
      fidelity.yaml
      drift.yaml
  iqm_tests/
    backend.py
    artifacts.py
    circuits/
      timing.py
      fidelity.py
      coherence.py
      crosstalk.py
      depth_limits.py
    analysis/
      timing_fit.py
      fidelity_fit.py
      qschedsim_export.py
    runners/
      run_campaign.py
      run_single.py
  data/
    raw/
    processed/
    reports/
```

## Script Implementation Plan

Build these scripts incrementally. Each script should be reviewed and tested
against the IQM machine or a dry-run backend before starting the next script.
Do not build the full suite first and test later.

Every executable script should:

- read `QFW_QC_URL` and `QFW_API_KEY`
- fail fast if required environment variables are missing
- write outputs under `data/raw/<date>/<script-name>/<run-id>/`
- use `HHMMSS` as the default `run-id`
- keep all artifacts from one script invocation under that run directory
- write generated circuits under the run directory's `circuits/`
- write execution results under the run directory's `results/`
- include software versions and calibration metadata in each run record
- support `--dry-run` when possible
- support `--shots`
- support `--output-dir`
- support `--run-id`
- use shared helpers from `scripts/qhw_util/` for paths, JSON writing, backend
  setup, serialization, and timing summaries

### 1. `scripts/env_check.py`

Purpose:

- validate `QFW_QC_URL`
- validate `QFW_API_KEY`
- create an IQM client
- confirm that the endpoint is reachable
- print sanitized machine identity
- never print the API key

Acceptance test:

- missing `QFW_QC_URL` fails clearly
- missing `QFW_API_KEY` fails clearly
- valid environment returns backend identity and exits zero

### 2. `scripts/discover.py`

Purpose:

- collect Phase 0 machine discovery data
- write `device_snapshot.json`
- write `coupling_graph.json`
- write `calibration_snapshot.json`
- write a first `qschedsim_device_skeleton.json`

Acceptance test:

- output files are created
- output includes qubits, native gates, coupling graph, calibration id, and
  available quality metrics
- missing optional IQM fields are recorded as missing, not treated as script
  failures

### 3. `scripts/submit_smoke.py`

Purpose:

- submit the smallest valid circuit
- retrieve counts
- record job id and timing metadata
- prove the result path works before running characterization campaigns

Acceptance test:

- one-qubit measured circuit returns counts
- output record includes job id, wall time, shots, circuit artifact paths, and
  calibration id

### 4. `scripts/timing_overhead.py`

Purpose:

- measure fixed job overhead
- measure shot-scaling behavior
- measure batch-size behavior
- separate client wall time from hardware timing if IQM exposes hardware
  timing metadata

Acceptance test:

- produces timing records for shot and batch sweeps
- writes a processed summary with fitted fixed and per-shot terms

### 5. `scripts/timing_1q.py`

Purpose:

- run repeated one-qubit native gates
- sweep qubits, gate types, and depths
- estimate per-qubit one-qubit gate duration

Acceptance test:

- produces per-qubit/per-gate timing records
- fit script can estimate slope and uncertainty

### 6. `scripts/timing_2q.py`

Purpose:

- run repeated two-qubit native gates
- sweep supported coupling edges and depths
- estimate per-edge two-qubit gate duration

Acceptance test:

- produces per-edge timing records
- fit script can estimate edge duration and uncertainty

### 7. `scripts/parallel_1q.py`

Purpose:

- test whether simultaneous one-qubit gate layers execute in parallel
- sweep active qubit count and depth
- detect width-dependent timing terms

Acceptance test:

- produces timing data for increasing active-qubit counts
- summary reports whether timing is constant or width-dependent

### 8. `scripts/parallel_2q.py`

Purpose:

- test whether disjoint two-qubit gates execute in parallel
- sweep edge matchings and repeated layer depths
- detect edge-set congestion terms

Acceptance test:

- produces timing data for one edge, multiple disjoint edges, and maximum
  matchings
- summary reports whether layer time is max-like, sum-like, or congested

### 9. `scripts/readout.py`

Purpose:

- characterize readout error
- measure readout scaling with number of measured qubits
- generate readout mitigation data

Acceptance test:

- produces per-qubit readout error
- produces readout correlation records
- records whether full assignment matrices are available for small subsets

### 10. `scripts/coherence.py`

Purpose:

- run T1 experiments
- run Ramsey T2 experiments
- run echo T2 experiments
- fit coherence parameters per qubit

Acceptance test:

- produces T1, T2 Ramsey, and T2 echo estimates with fit residuals
- output includes uncertainty estimates

### 11. `scripts/rb_1q.py`

Purpose:

- run single-qubit randomized benchmarking
- run simultaneous single-qubit randomized benchmarking
- optionally run interleaved RB for important one-qubit gates

Acceptance test:

- produces one-qubit fidelity estimates
- summary compares isolated and simultaneous RB

### 12. `scripts/rb_2q.py`

Purpose:

- run two-qubit randomized benchmarking per supported edge
- run simultaneous two-qubit randomized benchmarking on disjoint edges
- optionally run interleaved RB for native two-qubit gates

Acceptance test:

- produces per-edge fidelity estimates
- summary reports simultaneous-gate degradation

### 13. `scripts/depth_limits.py`

Purpose:

- run GHZ, mirror, Clifford, QV-like, and hardware-efficient ansatz circuits
- sweep width, depth, and qubit layout
- estimate useful circuit depth by circuit family

Acceptance test:

- produces depth-quality records
- reports depth thresholds under configured quality metrics

### 14. `scripts/crosstalk.py`

Purpose:

- run spectator-qubit tests
- run simultaneous-gate quality tests
- run readout crosstalk tests
- build a crosstalk matrix

Acceptance test:

- produces pairwise or edge-set crosstalk records
- summary identifies high-conflict qubits or edges

### 15. `scripts/drift_report.py`

Purpose:

- scan existing discovery outputs from scheduled `qhw_discover.sh` runs
- compare `data/<date>/discover/*/calibration_quality_summary.json`
  records over time
- track changes in T1, T2, readout fidelity, and gate fidelity

Acceptance test:

- report builds a time series from existing discover run directories
- summary identifies drift in T1, T2, readout, PRX, CZ, and Clifford metrics
- output does not submit jobs or query the backend directly

Operational note:

- drift data collection is handled by running `qhw_discover.sh` periodically,
  for example with cron or a systemd timer
- `drift_report.py` is a post-processing/reporting script, not a hardware
  submission script

### 16. `scripts/fit_timing_model.py`

Purpose:

- fit the execution-time formula for `qSchedSim`
- compare candidate models
- validate against held-out circuits

Acceptance test:

- writes fitted timing parameters
- writes model residuals
- reports held-out prediction error

### 17. `scripts/fit_noise_model.py`

Purpose:

- fit useful-depth and fidelity degradation models
- combine readout, gate-fidelity, coherence, and crosstalk results

Acceptance test:

- writes fitted noise parameters
- reports held-out quality prediction error

### 18. `scripts/export_qschedsim.py`

Purpose:

- export the measured device model into the `qSchedSim` schema
- include timing, topology, fidelity, readout, coherence, and drift metadata

Acceptance test:

- `qSchedSim` can load the exported device file
- exported model reproduces held-out timing predictions within the selected
  tolerance

### 19. `scripts/service_smoke.py`

Purpose:

- validate the QFw `svc_iqm_qpm` integration once it exists
- submit a small circuit through existing `api_qpm`
- prove IQM can be selected by `QFW_CAP_IQM`
- prove generic superconducting selection works through
  `QFW_CAP_SUPERCONDUCTING`

Acceptance test:

- IQM-specific reservation returns the IQM service
- superconducting reservation returns the IQM service when it is the only
  superconducting QPM
- circuit execution returns counts and IQM job metadata

### 20. `scripts/qec_memory.py`

Purpose:

- run a configurable quantum error correction memory experiment
- default to a rotated surface-code distance-3 memory test
- collect repeated syndrome samples and decode them offline
- validate whether the machine can support the circuit structure needed for
  error correction, even without real-time correction

Default target:

- distance: 3
- data qubits: `d^2 = 9`
- check/ancilla qubits: `d^2 - 1 = 8`
- total qubits: 17

A distance-3 rotated surface-code patch should fit on a 20-qubit system if
the coupling graph contains a suitable connected subgraph. Larger distances
should be rejected unless the backend has enough usable qubits.

Required backend capability checks:

- enough active qubits for the requested distance
- a connected physical patch that can embed the code layout
- supported two-qubit gates across all patch edges
- mid-circuit measurement support
- reset or reinitialization support for check qubits

If mid-circuit measurement or reset is not supported, the script should fail
clearly or switch to an explicitly labeled fallback mode. The fallback can be
an encode-idle-decode memory test, but it must not be reported as a full
repeated-round surface-code memory experiment.

Suggested command:

```bash
./qhw_qec_memory.sh \
    --distance 3 \
    --rounds 3 \
    --basis both \
    --shots 1000 \
    --decoder pymatching \
    --patch auto \
    --json
```

Core options:

- `--distance 3`
- `--rounds 1,3,5,7` or a single integer
- `--basis z|x|both`
- `--patch auto|QB1,QB2,...`
- `--shots 1000`
- `--repetitions 1`
- `--decoder pymatching|none`
- `--reset-mode hardware|none`
- `--idle-us <delay between rounds>`
- `--dry-run`

Workflow:

1. Query normalized backend metadata with `get_device_info()` and
   `get_coupling_graph()`.
2. Select or validate a physical surface-code patch.
3. Build the logical-to-physical mapping for data and check qubits.
4. Generate memory circuits for logical `Z` memory, logical `X` memory, or
   both.
5. Use a fixed stabilizer-extraction schedule that avoids using a qubit in
   two two-qubit gates in the same layer.
6. Submit circuits through the common backend wrapper.
7. Extract syndrome bits by shot and by round.
8. Build detection events offline from syndrome changes across rounds.
9. Decode detection events offline, initially with `pymatching` if available.
10. Report logical failure rate, detection-event rate by check, and
    detection-event rate by round.

The script should preserve the generated circuits as artifacts. For an
IQM/CZ-native backend, the circuit generator can express check extraction
through Qiskit gates and let the backend transpilation path lower it to native
operations, but the emitted artifacts should still record the selected patch
and schedule.

Expected output layout:

```text
data/<date>/qec_memory/<run-id>/
  patch.json
  backend_info.json
  coupling_graph.qhw.json
  circuits/
    qec_memory_z_d3_r3.qasm
    qec_memory_x_d3_r3.qasm
  results/
    qec_memory_z_d3_r3.qhw.json
    qec_memory_z_d3_r3.raw.json
    syndrome_records.jsonl
    decoder_records.jsonl
    analysis.json
    analysis.md
    qec_memory_summary.json
```

Analysis products:

- selected physical patch and check layout
- logical failure rate by basis
- syndrome detection-event rate by stabilizer
- detection-event rate by round
- hot checks or hot data qubits
- decoder success/failure counts
- fallback-mode warning when repeated-round QEC cannot be run

Acceptance test:

- dry-run generates a valid distance-3 patch, circuits, and analysis files
- hardware run either executes repeated-round circuits or fails with a clear
  unsupported-capability message
- decoded output includes logical failure rate for each requested basis
- all records are tied to backend metadata, coupling graph, and calibration id

## Campaign Order

Run campaigns in this order:

1. Machine discovery.
2. Connectivity and native gate validation.
3. Fixed overhead and shot-scaling tests.
4. Single-qubit timing.
5. Two-qubit timing.
6. Parallel gate timing.
7. Readout characterization.
8. T1 and T2 characterization.
9. Gate fidelity characterization.
10. Crosstalk characterization.
11. Depth-limit characterization.
12. Drift and repeatability campaign.
13. Fit and export `qSchedSim` device model.
14. Integrate and validate QFw IQM backend.

## Additional Items to Add

The original test list should be extended with:

- calibration snapshot capture before every campaign
- queue time versus hardware execution time separation
- shot-scaling and batch-scaling tests
- measurement and reset timing tests
- simultaneous-gate quality degradation tests
- crosstalk tests
- drift tests over hours and across calibrations
- readout mitigation data
- transpilation impact analysis
- layout sensitivity analysis
- connected-subgraph ranking
- per-edge two-qubit fidelity map
- process or state tomography on selected small circuits
- result reproducibility tests across repeated submissions
- QFw backend conformance tests
- `qSchedSim` export validation against circuits not used for fitting

## Acceptance Criteria

The characterization is complete enough for scheduling research when:

- the timing model predicts held-out circuit execution time within a chosen
  error bound
- the useful-depth model predicts held-out circuit quality within a chosen
  error bound
- model parameters include uncertainty or confidence intervals
- all data is tied to calibration set ids
- QFw can submit IQM circuits and retrieve results through a stable backend
- `qSchedSim` can import the measured device model
- documentation explains when the model is valid and when it must be
  regenerated
