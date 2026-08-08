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


def build_note_track(pcm, event_times, note_duration_ms, total_duration_s,
                     start_offset_s=0):
    """Place la tranche n de la musique au moment du n-ième événement.

    start_offset_s permet de sauter une intro : le découpage commence à cet
    endroit du morceau (et y reboucle si le morceau est épuisé).
    """
    note_samples = int(SAMPLE_RATE * note_duration_ms / 1000)
    offset = min(int(SAMPLE_RATE * start_offset_s), max(0, len(pcm) - note_samples))
    total_samples = int(SAMPLE_RATE * total_duration_s)
    track = np.zeros((total_samples, 2), dtype=np.int32)

    # Enveloppe anti-clic : fade-in bref, fade-out sur la moitié de la note.
    envelope = np.ones(note_samples)
    fade_in = int(SAMPLE_RATE * 0.008)
    fade_out = note_samples // 2
    envelope[:fade_in] = np.linspace(0, 1, fade_in)
    envelope[-fade_out:] = np.linspace(1, 0, fade_out)
    envelope = envelope[:, None]

    pos = offset
    for t in event_times:
        start = int(t * SAMPLE_RATE)
        if start >= total_samples:
            break
        if pos + note_samples > len(pcm):
            pos = offset  # la musique est épuisée : on reboucle
        note = (pcm[pos:pos + note_samples].astype(np.float32) * envelope).astype(np.int32)
        pos += note_samples
        end = min(start + len(note), total_samples)
        track[start:end] += note[:end - start]

    np.clip(track, -32768, 32767, out=track)
    return track.astype(np.int16)


def midi_to_freq(note):
    return 440.0 * 2 ** ((note - 69) / 12)


def load_midi_melody(path):
    """Extrait la mélodie d'un fichier MIDI (liste de fréquences en Hz).

    Algorithme "skyline" : toutes les pistes sauf la batterie sont fusionnées
    et, quand plusieurs notes démarrent en même temps (accord), on garde la
    plus aiguë — c'est presque toujours la mélodie chantée/connue.
    """
    import mido
    events = []
    t = 0.0
    for msg in mido.MidiFile(path):  # itération = temps en secondes, tempo géré
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and getattr(msg, "channel", 0) != 9:
            events.append((t, msg.note))
    events.sort()
    melody = []
    for start, pitch in events:
        if melody and start - melody[-1][0] < 0.035:
            if pitch > melody[-1][1]:
                melody[-1] = (start, pitch)
        else:
            melody.append((start, pitch))
    return [midi_to_freq(p) for _, p in melody]


def synth_note(freq, duration_s):
    """Note synthétisée façon piano électrique : attaque nette, décroissance douce."""
    n = int(SAMPLE_RATE * duration_s)
    t = np.arange(n) / SAMPLE_RATE
    env = np.exp(-t * 5.0) * np.minimum(1.0, t * 300)
    wave = (np.sin(2 * np.pi * freq * t)
            + 0.4 * np.sin(4 * np.pi * freq * t) * np.exp(-t * 8)
            + 0.2 * np.sin(6 * np.pi * freq * t) * np.exp(-t * 14))
    mono = (wave * env * 0.35 * 32767).astype(np.int32)
    return np.stack([mono, mono], axis=1)


def build_midi_track(freqs, event_times, note_duration_ms, total_duration_s):
    """Joue la note n de la mélodie au moment du n-ième événement."""
    total_samples = int(SAMPLE_RATE * total_duration_s)
    track = np.zeros((total_samples, 2), dtype=np.int32)
    # Les notes sonnent un peu plus longtemps que la tranche audio pour
    # laisser la décroissance respirer.
    duration = min(0.9, max(0.35, note_duration_ms / 1000 * 1.6))
    index = 0
    for t in event_times:
        start = int(t * SAMPLE_RATE)
        if start >= total_samples:
            break
        if not freqs:
            break
        note = synth_note(freqs[index % len(freqs)], duration)
        index += 1
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
