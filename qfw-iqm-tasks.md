# QFw-IQM Follow-Up Tasks

This file tracks the remaining work needed to move QFw-IQM from an
IQM-specific test repository toward a generic hardware characterization suite
with provider-specific backends.

## 1. Rename IQM-Specific Repository Concepts

The current repository, wrappers, scripts, and utility package still use `iqm`
in names even though the Qiskit-authored workflows are becoming generic.

Tasks:

- Decide the new repository/project name.
- Rename top-level shell wrappers away from `qhw_*`.
- Rename `scripts/*.py` workflows to provider-neutral names.
- Rename `scripts/qhw_util/` to a provider-neutral utility package.
- Remove the local QFw services config if QFw startup uses the active QFw
  service configuration.
- Rename config files such as `qhw_tests.yaml` if they are no longer
  IQM-specific.
- Keep compatibility shims only if they are useful during transition.

## 2. Remove Provider Calls From Generic Scripts

The `scripts/` directory should contain generic characterization workflows.
Provider-specific calls should live behind backend/provider implementations.

Tasks:

- Audit each script for direct IQM imports, IQM environment variables, IQM
  naming, IQM-specific result assumptions, and IQM-only metadata handling.
- Keep direct IQM logic inside the direct IQM backend/provider code.
- Keep QFw-specific service reservation and QPM interaction inside the QFw
  backend path.
- Ensure generic scripts only build circuits, select a backend, run tests, and
  consume normalized qhw data.
- Rename `_qasm.py` examples or clearly mark them as provider-specific
  debugging examples if they cannot be made generic.

## 3. Keep Backend Selection Clean And Extensible

The front end should not need to know whether the selected backend is direct
IQM, direct IBM, QFw-backed IQM, or another QFw-backed service.

Tasks:

- Maintain one script-facing backend wrapper API.
- Keep backend selection separate from test logic.
- For direct mode, select a provider backend based on user configuration.
- For QFw mode, select services by QFw type/capability/properties instead of
  provider-specific script logic.
- Define the minimal backend profile interface for adding a new direct
  hardware provider.
- Document how a new provider plugs into backend selection and result
  normalization.
- Fail clearly when a backend does not provide normalized qhw result data.

## 4. Continue Implementing The Test Plan

The current scripts cover environment checks, discovery, smoke submission,
fixed overhead timing, and 1Q timing. The remaining characterization scripts
from `test-plan.md` still need to be implemented.

Tasks:

- Implement the remaining timing and fidelity scripts one at a time.
- Update README documentation for every new script.
- Add each script to the manifest with an appropriate test level.
- Keep raw provider payloads and qhw-normalized artifacts for every run.
- Add post-processing summaries and plots where they make results easier to
  interpret.
- Validate each script in direct mode and QFw mode when hardware access is
  available.

## 5. Standardize Configuration Names And Environment Variables

Several current names include IQM and should become generic if the suite is
renamed.

Tasks:

- Decide which `QHW_*` variables should become provider-neutral.
- Preserve provider-specific variables only where they truly describe IQM
  behavior.
- Update wrapper scripts, manifests, README examples, and backend code
  together.
- Define a migration path so existing IQM runs do not break unexpectedly.

## 6. Define Provider Normalization Boundaries

The generic suite should consume qhw-normalized data. Provider-native parsing
should not leak into generic scripts.

Tasks:

- Keep `qhw-data` as the provider-neutral schema/building layer.
- Keep IQM-specific qhw conversion in `qhw-iqm`.
- For each new provider, create or select an equivalent normalizer package.
- Make direct backends normalize provider-native results before returning.
- Make QFw backends extract qhw-normalized data from QFw result metadata.
- Add tests for the normalized artifact shape expected by the generic scripts.

## 7. Add Regression Tests For The Backend Wrapper

The wrapper is now the critical abstraction point for all Qiskit-authored
workflows.

Tasks:

- Add unit tests for direct-result normalization.
- Add unit tests for QFw metadata extraction.
- Add tests for missing normalized data and missing raw payloads.
- Add tests for single-circuit and multi-circuit result records.
- Add dry-run coverage for scripts that use the backend wrapper.

## 8. Packaging And Distribution

If this repository becomes generic, it should be easy to install and use
outside the current development checkout.

Tasks:

- Decide whether this repo should become an installable Python package.
- If packaged, move reusable utilities into a stable package namespace.
- Keep shell wrappers usable from a source checkout.
- Decide whether provider backends should be optional extras.
- Update `requirements.txt` or `pyproject.toml` after the naming decision.
