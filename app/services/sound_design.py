"""Sample-preserving voice emphasis and deterministic, original ambience.

This is editing/mixing, not new actor performance or voice synthesis.
"""
import math
import wave
from pathlib import Path
import numpy as np
from app.services.provenance import atomic_json, file_sha256


def validate(plan, samples):
    if plan.get('version') != 1:
        raise ValueError('unsupported sound design version')
    for group in ('voice_gains', 'beds'):
        previous = 0
        for row in plan.get(group, []):
            a, b = row['sample_start'], row['sample_end']
            if type(a) is not int or type(b) is not int or not 0 <= a < b <= samples or a < previous:
                raise ValueError('sound events overlap or exceed the sample clock')
            previous = b
            limit = (-6, 3) if group == 'voice_gains' else (-50, -24)
            if not math.isfinite(row['db']) or not limit[0] <= row['db'] <= limit[1]:
                raise ValueError('sound gain outside safe authored range')
            if group == 'beds' and row.get('tone') not in ('tension', 'warm', 'resolve'):
                raise ValueError('unknown procedural ambience')


def envelope(n, sr, seconds):
    edge = min(n // 2, round(sr * seconds))
    env = np.ones(n, dtype=np.float64)
    if edge:
        ramp = np.sin(np.linspace(0, math.pi / 2, edge)) ** 2
        env[:edge] = ramp
        env[-edge:] = ramp[::-1]
    return env


def mix(master_path, plan, output_path):
    with wave.open(str(master_path), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError('directed sound requires mono PCM16 master')
        sr = wav.getframerate()
        source = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float64) / 32768
    validate(plan, len(source))
    voice = source.copy()
    for row in plan.get('voice_gains', []):
        a, b = row['sample_start'], row['sample_end']
        gain = 10 ** (row['db'] / 20)
        voice[a:b] *= 1 + (gain - 1) * envelope(b - a, sr, .08)
    bed = np.zeros_like(source)
    notes = {'tension': (110., 164.81, 220.), 'warm': (130.81, 196., 261.63),
             'resolve': (146.83, 220., 293.66)}
    for row in plan.get('beds', []):
        a, b = row['sample_start'], row['sample_end']
        t = np.arange(b - a) / sr
        frequencies = notes[row['tone']]
        # Soft bowed partials; local authored tones, no downloaded music or model.
        pad = sum(np.sin(2 * np.pi * f * t + .035 * np.sin(2 * np.pi * .17 * t))
                  + .16 * np.sin(2 * np.pi * f * 2 * t) for f in frequencies) / (len(frequencies) * 1.16)
        pad *= envelope(len(t), sr, min(2., len(t) / sr / 3))
        bed[a:b] += pad * 10 ** (row['db'] / 20)
    # Smooth sample-clock ducking from the existing voice, preserving all pauses.
    window = max(1, round(sr * .08))
    # O(n) rolling RMS, including clips shorter than the smoothing window.
    window = min(window, len(voice))
    padded = np.pad(voice * voice, (window // 2, (window - 1) // 2))
    sums = np.concatenate(([0.], np.cumsum(padded)))
    energy = np.sqrt(np.maximum(0., (sums[window:] - sums[:-window]) / window))
    duck = np.clip(1 - energy * 10, .35, 1)
    mixed = voice + bed * duck
    peak = float(np.max(np.abs(mixed)))
    scale = min(1., .85 / peak) if peak else 1.
    pcm = np.rint(mixed * scale * 32767).astype('<i2')
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), 'wb') as wav:
        wav.setparams((1, 2, sr, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm.tobytes())
    receipt = {'method': 'sample_preserving_edit_and_mix_v1', 'source_sha256': file_sha256(master_path),
               'output_sha256': file_sha256(target), 'samples': len(source), 'sample_rate': sr,
               'voice_identity': 'original_samples_no_pitch_or_time_change', 'global_peak_scale': scale,
               'plan': plan, 'listening_approved': False}
    atomic_json(target.with_suffix('.json'), receipt)
    return target
