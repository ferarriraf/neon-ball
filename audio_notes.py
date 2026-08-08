"""Construction de la bande-son : chaque événement joue la note suivante.

La musique source est découpée séquentiellement en tranches de
note_duration_ms : au 1er rebond la tranche 1, au 2e la tranche 2, etc.
C'est ce qui fait "jouer la mélodie" par la balle. Décodage et encodage
audio via le binaire ffmpeg embarqué par imageio-ffmpeg (rien à installer
sur le serveur).
"""

import logging
import subprocess
import wave

import numpy as np
from imageio_ffmpeg import get_ffmpeg_exe

log = logging.getLogger("audio")

SAMPLE_RATE = 44100


def decode_song(path, max_seconds=180):
    """Décode n'importe quel format audio en PCM stéréo 16 bits / 44,1 kHz.

    max_seconds borne la mémoire : la balle ne joue de toute façon que
    quelques dizaines de secondes de musique par vidéo.
    """
    cmd = [get_ffmpeg_exe(), "-v", "error", "-i", path,
           "-t", str(max_seconds),
           "-f", "s16le", "-acodec", "pcm_s16le",
           "-ac", "2", "-ar", str(SAMPLE_RATE), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
    if len(pcm) == 0:
        raise ValueError(f"Aucun audio décodé depuis {path}")
    return pcm


def build_note_track(pcm, event_times, note_duration_ms, total_duration_s):
    """Place la tranche n de la musique au moment du n-ième événement."""
    note_samples = int(SAMPLE_RATE * note_duration_ms / 1000)
    total_samples = int(SAMPLE_RATE * total_duration_s)
    track = np.zeros((total_samples, 2), dtype=np.int32)

    # Enveloppe anti-clic : fade-in bref, fade-out sur la moitié de la note.
    envelope = np.ones(note_samples)
    fade_in = int(SAMPLE_RATE * 0.008)
    fade_out = note_samples // 2
    envelope[:fade_in] = np.linspace(0, 1, fade_in)
    envelope[-fade_out:] = np.linspace(1, 0, fade_out)
    envelope = envelope[:, None]

    pos = 0
    for t in event_times:
        start = int(t * SAMPLE_RATE)
        if start >= total_samples:
            break
        if pos + note_samples > len(pcm):
            pos = 0  # la musique est épuisée : on reboucle au début
        note = (pcm[pos:pos + note_samples].astype(np.float32) * envelope).astype(np.int32)
        pos += note_samples
        end = min(start + len(note), total_samples)
        track[start:end] += note[:end - start]

    np.clip(track, -32768, 32767, out=track)
    return track.astype(np.int16)


def write_wav(path, pcm):
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())


def mux(video_path, wav_path, out_path):
    """Assemble la vidéo muette et la bande-son en un mp4 final."""
    cmd = [get_ffmpeg_exe(), "-y", "-v", "error",
           "-i", video_path, "-i", wav_path,
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", out_path]
    subprocess.run(cmd, check=True, capture_output=True)
