import importlib
import importlib.util
import io
import socket
import subprocess
import tempfile
from pathlib import Path
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image
from rss_transport import SafeFeedClient


def picture(fmt='PNG', size=(32, 32), color='red'):
    stream = io.BytesIO()
    Image.new('RGB', size, color).save(stream, format=fmt)
    return stream.getvalue()


@contextmanager
def media_server(body):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            seen.append(self.path)
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1/secret')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SafeFeedClient(resolver=lambda host, port: ['93.184.216.34'] if host == 'media.example' else [host],
        connector=lambda ip, port, timeout: socket.create_connection(server.server_address, timeout))
    try:
        yield client, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ThumbnailTests(unittest.TestCase):
    def test_oversized_video_is_rejected_before_frame_probe(self):
        from unittest.mock import patch
        import rss_thumbnail_worker as worker
        run = subprocess.run
        commands = []
        def record(command, **kwargs):
            commands.append(command)
            return run(command, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'wide.mp4'
            run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=4098x2:r=1:d=1',
                 '-c:v','libx264','-threads','1',str(path)],check=True)
            with path.open('rb') as source, tempfile.TemporaryFile() as output:
                with patch.object(subprocess,'run',side_effect=record), self.assertRaises(ValueError):
                    worker.decode_video(source,output)
        self.assertTrue(commands)
        self.assertFalse(any('-show_frames' in command for command in commands),
                         'source bounds must be checked before decoding frames')

    def test_real_animated_webp_preserves_sub10ms_durations(self):
        data = io.BytesIO()
        Image.new('RGB', (16,16), 'red').save(data, format='WEBP', save_all=True,
            append_images=[Image.new('RGB',(16,16),'blue')], duration=[1,8], lossless=True)
        with media_server(data.getvalue()) as (client, _):
            result = self.api().ThumbnailPreparer(client=client).prepare('http://media.example/fast')
        self.assertIsNotNone(result.thumbnail)
        self.assertEqual(result.thumbnail.durations_ms, (1,8))
        self.assertEqual(result.thumbnail.frames, (bytes([255,0,0])*256, bytes([0,0,255])*256))

    def test_ambiguous_and_nonmonotonic_integer_video_timing_rejected(self):
        import json
        from unittest.mock import patch
        import rss_thumbnail_worker as worker
        for timestamps, last_duration in (((0,0,2), 1), ((0,2,1), 2), ((0,1,2), 2)):
            frames = [dict(width=16, height=16, best_effort_timestamp=t) for t in timestamps]
            frames[-1]['pkt_duration'] = last_duration
            metadata = dict(frames=frames, streams=[dict(width=16,height=16,nb_frames=3,
                time_base='1/120',start_pts=0,duration_ts=3)])
            calls = []
            def probe(command, **kwargs):
                calls.append(command)
                kwargs['stdout'].write(json.dumps(metadata).encode())
            with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
                with patch.object(subprocess, 'run', side_effect=probe), self.assertRaises(ValueError):
                    worker.decode_video(source, output)
            self.assertTrue(calls)
            self.assertTrue(all(command[0] == '/usr/bin/ffprobe' for command in calls),
                            'invalid timing must fail before pixel decode')

    def test_decoder_wire_rejects_invalid_rational_pairs_and_frame_sizes(self):
        import os
        import struct
        from unittest.mock import patch
        import rss_thumbnails as api
        popen = subprocess.Popen
        for payload in (struct.pack('!II', 1,0)+bytes(768), struct.pack('!II',1,2)+bytes(768),
                        struct.pack('!II',60001,1)+bytes(768), bytes(775), bytes(776*301)):
            def spawn(command, **kwargs):
                code = f'import os; os.write({command[-1]}, {payload!r})'
                # Keep argv bounded even for the over-frame-limit case.
                code = (f'import os; os.write({command[-1]},bytes({len(payload)}))' if len(payload)>1000 else code)
                return popen([command[0], '-c', code], **kwargs)
            with media_server(picture()) as (client, _), patch.object(api.subprocess, 'Popen', side_effect=spawn):
                result = api.ThumbnailPreparer(client=client).prepare('http://media.example/a')
                self.assertIsNone(result.thumbnail)

    def test_decoder_cancel_deadline_reap_descendants_and_close_fds(self):
        import ctypes
        import os
        import time
        from unittest.mock import patch
        import rss_thumbnails as api
        libc = ctypes.CDLL(None)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)  # Test-only child subreaper.
        popen = subprocess.Popen
        try:
            for cancel in (True, False):
                children, descriptors = [], []
                reader, writer = os.pipe()
                stop = threading.Event()
                child_code = f'import os,time; os.write({writer}, str(os.getpid()).encode()+b"\\n"); time.sleep(60)'
                code = ('import rss_thumbnail_worker as w, subprocess, sys; '
                        f'w.decode=lambda s,o: subprocess.run(["/usr/bin/python3","-c",{child_code!r}],pass_fds=({writer},)); '
                        'w.main()')
                def spawn(command, **kwargs):
                    descriptors.extend(kwargs['pass_fds'])
                    kwargs['pass_fds'] += (writer,)
                    child = popen([command[0], '-c', code, *command[2:]], **kwargs)
                    children.append(child)
                    return child
                grandchild = None
                before = set(os.listdir('/proc/self/fd'))
                try:
                    with media_server(picture()) as (client, _), patch.object(api.subprocess, 'Popen', side_effect=spawn):
                        results = []
                        thread = threading.Thread(target=lambda: results.append(api.ThumbnailPreparer(
                            client=client, budget=1).prepare('http://media.example/a', stop)))
                        thread.start()
                        import select
                        self.assertTrue(select.select([reader], [], [], 2)[0])
                        grandchild = int(os.read(reader, 64).strip())
                        start = time.monotonic()
                        if cancel:
                            stop.set()
                        thread.join(2)
                        self.assertFalse(thread.is_alive())
                        self.assertIsNone(results[0].thumbnail)
                        self.assertLess(time.monotonic()-start, 1.5)
                    self.assertIsNotNone(children[0].returncode)
                    # Worker must reap its own subprocess, not orphan it to us/init.
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(grandchild, os.WNOHANG)
                    for fd in descriptors:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                    self.assertEqual(set(os.listdir('/proc/self/fd')), before)
                finally:
                    os.close(reader)
                    os.close(writer)
                    if grandchild:
                        try:
                            os.waitpid(grandchild, 0)
                        except ChildProcessError:
                            pass
        finally:
            libc.prctl(36, 0, 0, 0, 0)

    def test_fetch_cancel_and_deadline_close_real_socket(self):
        import time
        for cancel in (True, False):
            entered, closed, release = threading.Event(), threading.Event(), threading.Event()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    self.send_response(200)
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    entered.set()
                    self.connection.settimeout(2)
                    if self.connection.recv(1) == b'':
                        closed.set()
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            client = SafeFeedClient(resolver=lambda h,p: ['93.184.216.34'],
                connector=lambda ip,p,t: socket.create_connection(server.server_address,t))
            stop, results = threading.Event(), []
            worker = threading.Thread(target=lambda: results.append(self.api().ThumbnailPreparer(
                client=client, budget=3 if cancel else .2).prepare('http://media.example/stall', stop)))
            try:
                worker.start()
                self.assertTrue(entered.wait(1))
                start = time.monotonic()
                if cancel:
                    stop.set()
                worker.join(.7)
                self.assertFalse(worker.is_alive(), 'fetch did not promptly abort its socket')
                self.assertLess(time.monotonic()-start, .7)
                self.assertTrue(closed.wait(.3))
                self.assertIsNone(results[0].thumbnail)
            finally:
                worker.join(4)
                server.shutdown()
                server.server_close()
                server_thread.join()

    def test_real_120fps_and_vfr_pixels_and_literal_renderer_events(self):
        from fractions import Fraction
        from modules.rss_renderer import prepare_story, image_frames
        from rss_data import Story
        for vfr, expected in ((False, (Fraction(25, 3),) * 3),
                              (True, (Fraction(25, 3), Fraction(50, 3), Fraction(25, 3)))):
            with self.subTest(vfr=vfr), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'a.mp4'
                command = ['ffmpeg', '-v', 'error', '-f', 'rawvideo', '-pixel_format', 'rgb24',
                           '-video_size', '16x16', '-framerate', '120', '-i', 'pipe:0']
                if vfr:
                    command += ['-vf', 'setpts=if(eq(N,2),3,N)', '-fps_mode', 'passthrough']
                command += ['-c:v', 'libx264rgb', '-crf', '0', '-bf', '0', '-threads', '1', str(path)]
                # Escape filter expression commas for ffmpeg's filter parser.
                if vfr:
                    command[command.index('-vf')+1] = 'setpts=if(eq(N\\,2)\\,3\\,N)'
                colors = tuple(bytes(c)*256 for c in ((255,0,0), (0,255,0), (0,0,255)))
                subprocess.run(command, input=b''.join(colors), check=True, capture_output=True)
                with media_server(path.read_bytes()) as (client, _):
                    result = self.api().ThumbnailPreparer(client=client).prepare('http://media.example/v')
                self.assertEqual(result.warning, '')
                self.assertEqual(result.thumbnail.durations_ms, expected)
                self.assertEqual(result.thumbnail.frames, colors)
                story = prepare_story(Story((), 'A', '', '', None), thumbnail=result.thumbnail)
                frames = list(image_frames((story,)))
                times = [0., 1/120, 1/40 if vfr else 1/60, 1/30 if vfr else 1/40]
                self.assertEqual([f[2]['elapsed_seconds'] for f in frames[:4]], times)
                self.assertEqual([f[0].getpixel((0,0)) for f in frames[:4]],
                                 [(255,0,0),(0,255,0),(0,0,255),(255,0,0)])

    def test_exact_rational_admission_bounds(self):
        from fractions import Fraction
        from modules.rss_renderer import PreparedThumbnail
        frame = bytes(768)
        for delay in (1, 60000, Fraction(25,3), Fraction(100,3)):
            self.assertEqual(PreparedThumbnail((frame,), (delay,)).durations_ms, (delay,))
        for delay in (True, 1.0, Fraction(1,2), 60001, Fraction(2**33+1,2**32), Fraction(2**33,3)):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                PreparedThumbnail((frame,), (delay,))

    def test_real_adverse_media_and_unsafe_redirect_are_text_only(self):
        data = io.BytesIO()
        images = [Image.new('RGB', (1,1), (i%256, i//256, 0)) for i in range(301)]
        images[0].save(data, format='GIF', save_all=True, append_images=images[1:], duration=10)
        for body, path in ((b'x'*5_000_001, '/big'), (b'not media', '/bad'),
                           (b'\x00\x00\x00\x18ftypisom' + b'bad', '/bad.mp4'),
                           (picture(size=(4097,1)), '/wide'), (data.getvalue(), '/many'),
                           (picture(), '/redirect')):
            with self.subTest(path=path), media_server(body) as (client, seen):
                result = self.api().ThumbnailPreparer(client=client).prepare('http://media.example'+path)
                self.assertIsNone(result.thumbnail)
                self.assertTrue(result.warning)
                self.assertEqual(seen, [path])

    def test_all_video_tool_inputs_are_pipe_only_including_initial_probe(self):
        from unittest.mock import patch
        import rss_thumbnail_worker as worker
        run = subprocess.run
        commands = []
        def checked(command, **kwargs):
            commands.append(command)
            self.assertIn('-protocol_whitelist', command)
            self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'pipe')
            self.assertEqual(command[command.index('-enable_drefs') + 1], '0')
            self.assertEqual(command[command.index('-use_absolute_path') + 1], '0')
            return run(command, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'a.mp4'
            run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32:r=30:d=0.1',
                 '-c:v', 'libx264', '-threads', '1', str(path)], check=True)
            with path.open('rb') as source, tempfile.TemporaryFile() as output:
                with patch.object(subprocess, 'run', side_effect=checked):
                    worker.decode(source, output)
        self.assertGreaterEqual(len(commands), 2)

    def test_real_30fps_mp4_requires_fractional_source_cadence(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fractional.mp4'
            subprocess.run(['/usr/bin/ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=red:s=32x32:r=30:d=0.1', '-c:v', 'libx264', '-threads', '1',
                '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True)
            with media_server(path.read_bytes()) as (client, _):
                result = api.ThumbnailPreparer(client=client).prepare('http://media.example/movie.mp4')
                self.assertIsNotNone(result.thumbnail,
                    'Real 30fps MP4 rejected: integer-ms PreparedThumbnail cannot preserve source cadence')
                from fractions import Fraction
                self.assertEqual(result.thumbnail.durations_ms, (Fraction(100, 3),) * 3)

    def test_real_mp4_source_cadence_without_fps_resampling(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.mp4'
            subprocess.run(['/usr/bin/ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=red:s=32x32:r=25:d=0.12', '-c:v', 'libx264', '-threads', '1',
                '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True)
            with media_server(path.read_bytes()) as (client, _):
                result = api.ThumbnailPreparer(client=client).prepare('http://media.example/movie.mp4')
                self.assertEqual(result.warning, '')
                self.assertEqual(result.thumbnail.durations_ms, (40, 40, 40))
                self.assertTrue(all(frame[0] > 240 and frame[1] < 10 for frame in result.thumbnail.frames))

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('rss_thumbnails'), 'secure thumbnail preparation absent')
        return importlib.import_module('rss_thumbnails')

    def test_real_images_preserve_short_first_frame_cadence_and_color(self):
        api = self.api()
        for fmt in ('PNG', 'JPEG', 'WEBP'):
            with self.subTest(fmt=fmt), media_server(picture(fmt)) as (client, seen):
                result = api.ThumbnailPreparer(client=client).prepare('http://media.example/image')
                self.assertEqual(result.warning, '')
                self.assertEqual(len(result.thumbnail.frames), 1)
                self.assertGreater(result.thumbnail.frames[0][0], 240)
                self.assertLess(result.thumbnail.frames[0][1], 10)
                self.assertEqual(seen, ['/image'])
        data = io.BytesIO()
        Image.new('RGB', (16, 16), 'red').save(data, format='GIF', save_all=True,
            append_images=[Image.new('RGB', (16, 16), 'blue')], duration=[10, 70], loop=0)
        with media_server(data.getvalue()) as (client, _):
            result = api.ThumbnailPreparer(client=client).prepare('http://media.example/a.gif')
            self.assertEqual(result.warning, '')
            self.assertEqual(result.thumbnail.durations_ms, (10, 70))
            self.assertEqual(result.thumbnail.frames, (bytes([255, 0, 0])*256, bytes([0, 0, 255])*256))
