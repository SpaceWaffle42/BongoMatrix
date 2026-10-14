"""Descriptor-only RSS decoder entrypoint; executed in a disposable process group."""
import os
import json
from fractions import Fraction
import resource
import signal
import struct
import subprocess
import sys
import tempfile
from PIL import Image, ImageSequence
from media_sanitizer import FORMAT_BY_EXTENSION, drop_privileges


def dimensions(size):
    if any(not 1 <= dimension <= 4096 for dimension in size):
        raise ValueError('Source dimensions exceed RSS policy')


def decode(source, output):
    header = source.read(12)
    source.seek(0)
    if header[4:8] == b'ftyp':
        return decode_video(source, output)
    with Image.open(source) as image:
        if image.format not in set(FORMAT_BY_EXTENSION.values()):
            raise ValueError('Unsupported content format')
        dimensions(image.size)
        count = getattr(image, 'n_frames', 1)
        if not 1 <= count <= 300:
            raise ValueError('Too many frames')
        for frame in ImageSequence.Iterator(image):
            dimensions(frame.size)
            frame.load()
            dimensions(frame.size)
            delay = frame.info.get('duration', 100) if count > 1 else 100
            if type(delay) not in (int, float) or int(delay) != delay or not 1 <= delay <= 60000:
                raise ValueError('Source cadence cannot be represented')
            rgba = frame.convert('RGBA')
            rgba.thumbnail((16, 16), Image.Resampling.LANCZOS)
            tile = Image.new('RGB', (16, 16))
            tile.paste(rgba, ((16-rgba.width)//2, (16-rgba.height)//2), rgba)
            output.write(struct.pack('!II', int(delay), 1) + tile.tobytes())


def decode_video(source, output):
    fd = source.fileno()
    # Never permit network protocols or external data references from a container.
    input_args = ['-protocol_whitelist', 'pipe', '-f', 'mov', '-enable_drefs', '0',
                  '-use_absolute_path', '0', '-i', f'pipe:{fd}']
    with tempfile.TemporaryFile() as probe:
        os.lseek(fd, 0, os.SEEK_SET)
        subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-threads', '1', *input_args,
            '-select_streams', 'v:0', '-show_streams', '-show_entries',
            'stream=width,height,nb_frames,time_base,start_pts,duration_ts',
            '-of', 'json'], pass_fds=(fd,), stdout=probe, stderr=subprocess.DEVNULL, check=True)
        probe.seek(0)
        metadata = json.loads(probe.read(2_000_001))
        video = metadata['streams'][0]
    dimensions((int(video.get('width', 0)), int(video.get('height', 0))))
    if int(video.get('nb_frames', 1)) > 300:
        raise ValueError('Too many frames')
    with tempfile.TemporaryFile() as probe:
        os.lseek(fd, 0, os.SEEK_SET)
        subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-threads', '1', *input_args,
            '-select_streams', 'v:0', '-show_frames', '-show_entries',
            'frame=width,height,best_effort_timestamp,pkt_duration', '-of', 'json'],
            pass_fds=(fd,), stdout=probe, stderr=subprocess.DEVNULL, check=True)
        probe.seek(0)
        frames = json.loads(probe.read(2_000_001))['frames']
    if not 1 <= len(frames) <= 300:
        raise ValueError('Too many decoded frames')
    delays = []
    time_base = Fraction(video['time_base'])
    if time_base <= 0:
        raise ValueError('Invalid time base')
    for index, frame in enumerate(frames):
        dimensions((int(frame['width']), int(frame['height'])))
        timestamp = int(frame['best_effort_timestamp'])
        if index + 1 < len(frames):
            duration = int(frames[index + 1]['best_effort_timestamp']) - timestamp
        else:
            endpoint = int(video['start_pts']) + int(video['duration_ts'])
            duration = int(frame.get('pkt_duration', endpoint - timestamp))
            if timestamp + duration != endpoint:
                raise ValueError('Ambiguous final frame duration')
        delay = duration * time_base * 1000
        if not 1 <= delay <= 60000 or delay.numerator > 0xffffffff or delay.denominator > 0xffffffff:
            raise ValueError('Video cadence cannot be represented by prepared contract')
        delays.append(delay)
    with tempfile.TemporaryFile() as raw:
        os.lseek(fd, 0, os.SEEK_SET)
        subprocess.run(['/usr/bin/ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', *input_args,
            '-map', '0:v:0', '-an', '-sn', '-dn', '-vf',
            'scale=16:16:force_original_aspect_ratio=decrease,pad=16:16:(ow-iw)/2:(oh-ih)/2',
            '-vsync', '0', '-frames:v', '301', '-threads', '1', '-pix_fmt', 'rgb24',
            '-f', 'rawvideo', 'pipe:1'], pass_fds=(fd,), stdout=raw,
            stderr=subprocess.DEVNULL, check=True)
        raw.seek(0)
        pixels = raw.read(301 * 768 + 1)
    if len(pixels) != len(delays) * 768:
        raise ValueError('Decoded frame count mismatch')
    for index, delay in enumerate(delays):
        output.write(struct.pack('!II', delay.numerator, delay.denominator) + pixels[index*768:(index+1)*768])


def main():
    def terminate(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit('Thumbnail cancelled')
    # subprocess.run unwinds by killing AND waiting for its tool child.
    signal.signal(signal.SIGTERM, terminate)
    Image.init()
    drop_privileges()
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2_000_000, 2_000_000))
    with os.fdopen(os.dup(int(sys.argv[1])), 'rb') as source:
        with os.fdopen(os.dup(int(sys.argv[2])), 'wb') as output:
            decode(source, output)


if __name__ == '__main__':
    main()
