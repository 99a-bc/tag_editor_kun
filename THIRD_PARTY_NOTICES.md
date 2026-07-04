# Third-Party Notices

## a1111-sd-webui-tagcomplete — Danbooru completion dictionary

This project includes `resources/dictionaries/danbooru.csv`, a fixed snapshot derived from
`tags/danbooru.csv` in the following project.

- Project: `DominikDoom/a1111-sd-webui-tagcomplete`
- Source file: `tags/danbooru.csv`
- License: MIT License
- Copyright (c) 2022 Dominik Reh

The application does not download or update this dictionary automatically.
When updating the bundled dictionary, update this notice together with the CSV.

This notice describes the license information for the source file included through
a1111-sd-webui-tagcomplete. It does not claim ownership of the underlying
Danbooru-derived tag data or guarantee separate rights that may apply to its origin.

### MIT License

Copyright (c) 2022 Dominik Reh

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Runtime-downloaded tagger models

This project supports the following image tagger models. Their model weights and
model-provided files are **not** bundled with this repository or its releases.
They are obtained from their respective Hugging Face repositories into the end
user's local cache.

Use of each model is subject to the license and any access conditions shown by
its source repository. This project does not grant rights to the model artifacts
or to the underlying training data.

### WD EVA02 Large Tagger v3

- Model repository: `SmilingWolf/wd-eva02-large-tagger-v3`
- Source: `https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3`
- License listed by the model card: Apache License 2.0

### PixAI Tagger v0.9

- Model repository: `pixai-labs/pixai-tagger-v0.9`
- Source: `https://huggingface.co/pixai-labs/pixai-tagger-v0.9`
- License listed by the model card for the weights: Apache License 2.0
- Access: The end user must review and accept any access conditions required by
  the source repository before downloading the model files.
