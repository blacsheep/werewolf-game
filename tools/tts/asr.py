#!/usr/bin/env python3
"""Transcribe audio files with faster-whisper (used only by verify.py --asr). Prints JSON list."""
import argparse
import json
import subprocess

import numpy as np
from faster_whisper import WhisperModel


def load(path):
    # decode with ffmpeg: recent PyAV releases break faster_whisper.decode_audio
    pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 's16le', '-ac', '1', '-ar', '16000', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0


parser = argparse.ArgumentParser()
parser.add_argument('--model', default='small')
parser.add_argument('--model-dir')
parser.add_argument('files', nargs='+')
args = parser.parse_args()
model = WhisperModel(args.model, device='cpu', compute_type='int8', download_root=args.model_dir)
out = []
for path in args.files:
    segments, _ = model.transcribe(load(path), language='zh', beam_size=1, vad_filter=False,
                                   initial_prompt='以下是普通话的狼人杀发言，座位号写作几号。')
    out.append({'file': path, 'text': ''.join(s.text for s in segments).strip()})
print(json.dumps(out, ensure_ascii=False))
