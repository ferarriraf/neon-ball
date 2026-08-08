"""Génération d'une vidéo ball escape complète : simulation, rendu, son, mux."""

import logging
import os
import random
import subprocess
import time

import pygame
from imageio_ffmpeg import get_ffmpeg_exe

from ballescape import Simulation

log = logging.getLogger("generator")

MUSIC_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus")
MIDI_EXTENSIONS = (".mid", ".midi")
PHYSICS_SUBSTEPS = 6


def pick_song(music_dir):
    if not os.path.isdir(music_dir):
        return None
    files = [f for f in os.listdir(music_dir)
             if f.lower().endswith(MUSIC_EXTENSIONS + MIDI_EXTENSIONS)]
    if not files:
        return None
    return os.path.join(music_dir, random.choice(files))


def generate_video(config, out_dir):
    """Génère une vidéo ; retourne (chemin_mp4, nom_musique) ou None."""
    video_cfg = config.get("video", {})
    music_cfg = config.get("music", {})
    width = video_cfg.get("width", 1080)
    height = video_cfg.get("height", 1920)
    fps = video_cfg.get("fps", 60)
    duration = video_cfg.get("duration_seconds", 68)
    ring_count = video_cfg.get("rings", 10)
    note_ms = music_cfg.get("note_duration_ms", 320)

    song_path = pick_song(music_cfg.get("dir", "musics"))
    if not song_path:
        log.error("Aucune musique trouvée dans le dossier '%s' : dépose des fichiers "
                  "%s via le gestionnaire de fichiers.",
                  music_cfg.get("dir", "musics"), "/".join(MUSIC_EXTENSIONS))
        return None
    song_name = os.path.splitext(os.path.basename(song_path))[0]
    is_midi = song_path.lower().endswith(MIDI_EXTENSIONS)
    log.info("Musique choisie : %s%s", song_name, " (MIDI)" if is_midi else "")

    # Vérifie que le fichier est lisible AVANT de lancer un long rendu.
    melody = None
    if is_midi:
        from audio_notes import load_midi_melody
        try:
            melody = load_midi_melody(song_path)
        except Exception as exc:
            log.error("Fichier MIDI illisible (%s) : %s", song_path, exc)
            return None
        if not melody:
            log.error("Aucune note trouvée dans le MIDI %s", song_path)
            return None
        log.info("Mélodie extraite : %d notes", len(melody))
    else:
        probe = subprocess.run(
            [get_ffmpeg_exe(), "-v", "error", "-t", "1", "-i", song_path, "-f", "null", "-"],
            capture_output=True,
        )
        if probe.returncode != 0:
            log.error("Musique illisible (%s) : %s", song_path,
                      probe.stderr.decode(errors="replace")[:200])
            return None

    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw_video = os.path.join(out_dir, f"ball-{stamp}-mute.mp4")
    wav_path = os.path.join(out_dir, f"ball-{stamp}.wav")
    final_path = os.path.join(out_dir, f"ball-{stamp}.mp4")

    encoder = subprocess.Popen(
        [get_ffmpeg_exe(), "-y", "-v", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
         "-c:v", "libx264",
         "-preset", video_cfg.get("encoder_preset", "veryfast"), "-crf", "20",
         # Sans limite, x264 lance un thread par cœur de la machine HÔTE :
         # sur un petit conteneur ça explose la RAM et ffmpeg se fait tuer.
         "-threads", str(video_cfg.get("encoder_threads", 2)),
         "-pix_fmt", "yuv420p", raw_video],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    sim = Simulation(width, height, ring_count, random.Random())
    surface = pygame.Surface((width, height))
    total_frames = duration * fps
    dt = 1.0 / (fps * PHYSICS_SUBSTEPS)
    events = []
    started = time.time()

    log.info("Rendu de %ds à %dx%d %dfps (%d frames)...",
             duration, width, height, fps, total_frames)
    progress_step = max(1, total_frames // 10)
    try:
        try:
            for frame in range(total_frames):
                t = frame / fps
                for sub in range(PHYSICS_SUBSTEPS):
                    events.extend(sim.step(dt, t + sub * dt))
                sim.render(surface, t)
                encoder.stdin.write(pygame.image.tostring(surface, "RGB"))
                if frame and frame % progress_step == 0:
                    elapsed = time.time() - started
                    log.info("Rendu %d%% (%.0fs écoulées, ~%.0fs restantes)",
                             100 * frame // total_frames, elapsed,
                             elapsed * (total_frames - frame) / frame)
            encoder.stdin.close()
        except (BrokenPipeError, OSError):
            stderr = encoder.stderr.read().decode(errors="replace")[-400:]
            raise RuntimeError(
                "L'encodeur vidéo s'est arrêté en plein rendu (RAM insuffisante ? "
                f"réduis width/height/fps dans config.json). ffmpeg : {stderr or 'tué sans message'}")
        if encoder.wait() != 0:
            stderr = encoder.stderr.read().decode(errors="replace")[-400:]
            raise RuntimeError(f"ffmpeg a échoué pendant l'encodage : {stderr}")

        log.info("%d notes jouées, construction de la bande-son...", len(events))
        # Import et décodage seulement après le rendu : sur les petits
        # serveurs, ça évite de garder numpy + le PCM en RAM pendant
        # que l'encodeur vidéo travaille.
        from audio_notes import (decode_song, build_note_track, build_midi_track,
                                 write_wav, mux)
        if is_midi:
            track = build_midi_track(melody, events, note_ms, duration)
        else:
            start_offset = music_cfg.get("start_offset_seconds", 0)
            pcm = decode_song(song_path, max_seconds=start_offset + 180)
            track = build_note_track(pcm, events, note_ms, duration, start_offset)
        write_wav(wav_path, track)
        mux(raw_video, wav_path, final_path)
    finally:
        if encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        for temp in (raw_video, wav_path):
            if os.path.exists(temp):
                os.remove(temp)

    log.info("Vidéo générée en %.0fs : %s", time.time() - started, final_path)
    return final_path, song_name
