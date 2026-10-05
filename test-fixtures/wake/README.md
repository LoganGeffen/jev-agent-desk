These mono 16 kHz signed little-endian PCM fixtures were synthesized locally with
Piper 1.3.0 and `en_US-ljspeech-medium` (MIT voice model; public-domain LJSpeech
training corpus). No recordings of a person or cloud speech services were used.
The model card is https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/ljspeech/medium/MODEL_CARD .

- positive: “Hey Jarvis.”
- command: “Hey Jarvis, read Dog Data.”
- negative: “Please explain those numbers with an example.”
- confusing: “Hey Marvin, read the notes.”

A single synthesized speaker is a regression fixture, not evidence of physical
phone, accent, noise, or speaker-echo performance.
