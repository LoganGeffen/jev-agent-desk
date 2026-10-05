# Vendored wake dependencies

`PROVENANCE.json` records exact published versions, asset hashes and model source.
`tools/wake-build/package-lock.json` pins the npm distribution integrity values.
No dependency fetch occurs at runtime outside this app's origin.

The runtime contains uww.js 0.2.1 (MIT), TensorFlow.js core/CPU 4.9.0 and
TensorFlow.js TFLite 0.0.1-alpha.10 (Apache-2.0, The TensorFlow Authors).
The included TensorFlow TFLite WASM distribution is from that pinned npm package.
The uww audio frontend includes TensorFlow Lite Micro (Apache-2.0) and Kiss FFT
(BSD-3-Clause); their notices are retained in NOTICE-UWW. Its recorded
pymicro-features upstream release is 2.0.2.

The Apache license is retained as LICENSE-APACHE-2.0; package-distributed MIT/BSD
licenses and notices are retained under dependency-licenses/. Some TensorFlow
npm packages declare Apache-2.0 without distributing a separate license file;
the shared complete Apache license applies to these entries. The model's
Apache-2.0 grant explicitly includes weights and manifests, as stated in its
pinned upstream README (see PROVENANCE.json).

Packaging changes: alias the package's broken TensorFlow TFLite `module` entry
to its own FESM distribution, and redirect its one import-time empty-path WASM
probe to `/wake-assets/wasm/`. The normal model loader also specifies that path.
No detector thresholds or model weights have been changed. To rebuild, run
`npm ci --ignore-scripts && node build.mjs` in tools/wake-build. Model and license
files are copied from the pinned upstream model repository.
