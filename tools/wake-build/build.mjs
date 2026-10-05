import {build} from 'esbuild';
import {readFileSync, writeFileSync, cpSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
const root = fileURLToPath(new URL('../../', import.meta.url));
const here = fileURLToPath(new URL('./', import.meta.url));
const output = resolve(root, 'wake-assets/runtime.js');
await build({stdin: {contents: "export {InferencePipeline, Detector} from 'uww.js';", resolveDir: here},
  bundle: true, minify: true, format: 'esm', outfile: output,
  alias: {'@tensorflow/tfjs-tflite': resolve(here, 'node_modules/@tensorflow/tfjs-tflite/dist/tf-tflite.fesm.js')}});
const source = readFileSync(output, 'utf8');
const probe = 'getInstance("","tflite_web_api"';
if (source.split(probe).length !== 2) throw new Error('Expected exactly one upstream import-time WASM probe');
// Upstream probes WASM before load(); both paths must stay on our origin.
writeFileSync(output, source.replace(probe, 'getInstance("/wake-assets/wasm/","tflite_web_api"'));
cpSync(resolve(here, 'node_modules/@tensorflow/tfjs-tflite/wasm'), resolve(root, 'wake-assets/wasm'), {recursive: true});
