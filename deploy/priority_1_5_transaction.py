#!/usr/bin/env python3
"""Crash-safe, source-only Priority 1.5 B1 release transaction."""
from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Callable

from PIL import Image, ImageSequence

_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))


class _StoreOnce(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_priority_1_5_seen", None)
        if seen is None:
            seen = set()
            setattr(namespace, "_priority_1_5_seen", seen)
        if self.dest in seen:
            parser.error(f"{option_string} may be specified only once")
        seen.add(self.dest)
        setattr(namespace, self.dest, values)


def add_store_once_argument(parser, name, **kwargs):
    kwargs["action"] = _StoreOnce
    parser.add_argument(name, **kwargs)

GIT = "/usr/bin/git"
SYSTEMCTL = "/usr/bin/systemctl"
FFPROBE = "/usr/bin/ffprobe"
JOURNALCTL = "/usr/bin/journalctl"
DISPLAY_LOCK = Path("/run/matrix-display/matrix-display.lock")
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SERVICE = re.compile(r"[A-Za-z0-9@_.:-]+\.service")
FORBIDDEN_PREFIXES = ("data/", "venv/", ".git/", "resources/media/")
STATE_KEYS = {
    "version", "boundary", "baseline_sha", "baseline_tree_sha", "source_sha", "tree_sha",
    "release_root", "release_manifest", "fixture_manifest", "baseline_manifest",
    "destination", "destination_owner", "backup_root", "service", "service_baseline",
    "interpreter", "state_path", "self",
}
PUBLIC_FINAL_KEYS = {
    "version", "boundary", "accepted_source_sha", "accepted_tree_sha",
    "release_manifest_sha256", "baseline_manifest_sha256", "status",
}

# Canonical multi-frame media, generated once and embedded so build-b1 never
# depends on a host encoder, Pillow writer, or encoder-version metadata.
_FIXTURE_SPECS = (
    ('fixtures/b1-landscape-loop.gif', 'gif', 'eeca6314667ff792803738970918d13bb967f1855a9eb35cfaf7e2c0435b1483', base64.b64decode(
        'R0lGODlhQQAhAIEAACPh4dweHgAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQIBgAAACwAAAAAQQAhAAAIkAABBBhIsKDAgggP'
        'IiSocGGAhgshOpxIsaLFixgzatzIkWFFiQY/iqQIsqPJkyhTqhw5seRAlw9ZOoS5sqbNmzhh6pQZkSfOn0CDctxJ0mfIokKTKl2a'
        '0KhHpC2dMp1KFSXRqFBnSq3KtavFq1qz9hTrtazZsVjThlWL9qxbt2Dbym1K9q3dqnHpstV7ty/VgAAh+QQIDgAAACwAAAAAQQAh'
        'AIHrS8MUtDwAAAAAAAAIjgADCBxIsKDBgwgTKlyIEIBChwkhNnxIkaHFixgzatzIEaPEgx8Nhiw4kmDJjihTqlyp8qRAlwFgyqzIs'
        'qbNmzgHzoxIE2TPnECDCv1JkqhJozqHKl2qdOdEnlCfMp1KdaVTn1GxSq3KtevFqyKRvhTrtWxZsEWzhlVrtu1ZsmiPsnVLl2rcp'
        'HPxbq1rMSAAIfkECB4AAAAsAAAAAEEAIQCB4bkjHkbcAAAAAAAACI8AAwgcSLCgwYMIEypcyLChw4cQAwBQODFhRYQXD2Y0uDGix'
        '48gQ4r02JFgyYEnBaaUSHGky5cwYz5cSbOlRZsyc+rcCbLmzZ8YeQodSpQjTo1HjQItyrQpTJ9BlyqN6rSqVZJJC0JFKvWq169Tu'
        'VIVSzYs2LNgt5rVmtUk2rdp26KUq5Iu3Ls81bLt6hZoQAA7'
    )),
    ('fixtures/b1-landscape.mp4', 'mp4', '93a4b38eaf4e1fb6036c5f735958637bcb954bc1f3897f843bc84459051ae7de', base64.b64decode(
        'AAAAJGZ0eXBpc29tAAACAGlzb21pc282aXNvMmF2YzFtcDQxAAACvW1vb3YAAABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAAAAAEA'
        'AAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
        'AAIAAAHkdHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAA'
        'AQAAAAAAAAAAAAAAAAAAQAAAAAALAAAABQAAAAABgG1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAKAAAAAAAVcQAAAAAAC1oZGxy'
        'AAAAAAAAAAB2aWRlAAAAAAAAAAAAAAAAVmlkZW9IYW5kbGVyAAAAASttaW5mAAAAFHZtaGQAAAABAAAAAAAAAAAAAAAkZGluZgAA'
        'ABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAADrc3RibAAAAJ9zdHNkAAAAAAAAAAEAAACPYXZjMQAAAAAAAAABAAAAAAAAAAAA'
        'AAAAAAAAAAALAAUASAAAAEgAAAAAAAAAAQ9MYXZjIGxpYngyNjRyZ2IAAAAAAAAAAAAAAAAAAAAAABj//wAAADlhdmNDAfQACv/h'
        'ABxn9AAKkcmEX5oybgQEAIAAAAMAgAAABQeJEwjAAQAGaOhDAa8s//j4AAAAABBzdHRzAAAAAAAAAAAAAAAQc3RzYwAAAAAAAAAA'
        'AAAAFHN0c3oAAAAAAAAAAAAAAAAAAAAQc3RjbwAAAAAAAAAAAAAAKG12ZXgAAAAgdHJleAAAAAAAAAABAAAAAQAAAAAAAAAAAAAA'
        'AAAAAD11ZHRhAAAANW1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAACGlsc3QAAAB8bW9vZgAAABBt'
        'ZmhkAAAAAAAAAAEAAABkdHJhZgAAACR0ZmhkAAAAOQAAAAEAAAAAAAAC4QAACAAAAALTAQEAAAAAABR0ZmR0AQAAAAAAAAAAAAAA'
        'AAAAJHRydW4AAAIFAAAAAwAAAIQCAAAAAAAC0wAAANAAAADZAAAEhG1kYXQAAAIABgX//NxF6b3m2Ui3lizYINkj7u94MjY0IC0g'
        'Y29yZSAxNjUgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDI1IC0gaHR0cDovL3d3dy52aWRlb2xh'
        'bi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MTYgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDEzMyBt'
        'ZT11bWggc3VibWU9OSBwc3k9MCBtaXhlZF9yZWY9MSBtZV9yYW5nZT0yNCBjaHJvbWFfbWU9MSB0cmVsbGlzPTAgOHg4ZGN0PTEg'
        'Y3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0wIGNocm9tYV9xcF9vZmZzZXQ9MCB0aHJlYWRzPTEgbG9va2FoZWFkX3Ro'
        'cmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0'
        'cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0wIHdlaWdodHA9MiBrZXlpbnQ9OTk5OSBrZXlpbnRfbWluPTUgc2NlbmVjdXQ9NDAgaW50'
        'cmFfcmVmcmVzaD0wIHJjPWNxcCBtYnRyZWU9MCBxcD0wAIAAAADLZYiCV9hfX+O/lINK8MT31rtH3pe32is3XK1rrmuVaB64Mer4'
        'Btvx0oaACPymw3C9DBtvYhcDGi1j+MK7RXQAjDTMtFDpIpbQPO1TmPSEwazFA/QsdKj8XvPa6sIAvmRXTtt/sorxHhMKf909YX0u'
        'aWqfNuy8yiB4qp/8oZgDWvvmOOLF7gFhfVIItKEShzVBTtvBzxssgubtscWmjec7+R4SH8hfxcMb6pB2qZTGBPRlUNUwKxeKxOPE'
        '+XC1C652ZprrG/hzuLovP8EAAADMQYiFX9hfX+O/lINLixxbbdEgayd81TKsckITXQWhlfNvLK1/a9V1ZXAwTlT7X5o8ilNenNiy'
        'MJVa07EpYuPOACsf+HOCARVE9Qxr8wx0iAbuDtdauv/qJwkluQnksl3RLkJd3xlF478SyrCQu+zxnsWHOu5Tf+Cb/k6NTnPzafQU'
        '6FlCgZoIccWNBwYc7l+vgJjbxQccE3eHec/LYr+6ZU7x23f2Lyl/VRx81F92okaQ6Gqw2MovFYnHifLhahdc7M011jfw53F0Xn+A'
        'AAAA1UGIiV/YX1/jv5SDS6M94A+KWVNZqnvkwvpZ1RirOXrVle8e11V1ZULkPU4nRGATXMqZP9u4M/li19cBiy+kNxVFIpj0ToYC'
        '9tIltvCR/EzLNEF7liekg6nV3JSW3sQSbytkOI2mTusQVky8p8SyrXiibFksZ7G0UwtNuCiyX/rq4ZTlCgZoIccWNGYWzaVQ9V91'
        'L5yxQ5TAAwRmy7iX87pIWPOMTplTvHbc+LEzpRSjljv6JwQxM23nmwf6tLqxOSx+KlV81C658sryl3nG/hzuLovP8QAAAENtZnJh'
        'AAAAK3RmcmEBAAAAAAAAAQAAAAAAAAABAAAAAAAAAAAAAAAAAAAC4QEBAQAAABBtZnJvAAAAAAAAAEM='
    )),
    ('fixtures/b1-portrait-finite.gif', 'gif', '4ac60e3e0b6d5dd62113c45deda6d3ada52e4f9c362ce17b348d31b9941acbd9', base64.b64decode(
        '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'
    )),
    ('fixtures/b1-portrait.mp4', 'mp4', 'f462de666a87cc665d4b6786206ff04ecae9a4375e9dc8a1aa3d61653123f8fc', base64.b64decode(
        'AAAAJGZ0eXBpc29tAAACAGlzb21pc282aXNvMmF2YzFtcDQxAAACvm1vb3YAAABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAAAAAEA'
        'AAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
        'AAIAAAHldHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAA'
        'AQAAAAAAAAAAAAAAAAAAQAAAAAAFAAAACQAAAAABgW1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAKAAAAAAAVcQAAAAAAC1oZGxy'
        'AAAAAAAAAAB2aWRlAAAAAAAAAAAAAAAAVmlkZW9IYW5kbGVyAAAAASxtaW5mAAAAFHZtaGQAAAABAAAAAAAAAAAAAAAkZGluZgAA'
        'ABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAADsc3RibAAAAKBzdHNkAAAAAAAAAAEAAACQYXZjMQAAAAAAAAABAAAAAAAAAAAA'
        'AAAAAAAAAAAFAAkASAAAAEgAAAAAAAAAAQ9MYXZjIGxpYngyNjRyZ2IAAAAAAAAAAAAAAAAAAAAAABj//wAAADphdmNDAfQACv/h'
        'AB1n9AAKkcmEX4yIm4EBACAAAAMAIAAAAwKB4kTCMAEABmjoQwGvLP/4+AAAAAAQc3R0cwAAAAAAAAAAAAAAEHN0c2MAAAAAAAAA'
        'AAAAABRzdHN6AAAAAAAAAAAAAAAAAAAAEHN0Y28AAAAAAAAAAAAAAChtdmV4AAAAIHRyZXgAAAAAAAAAAQAAAAEAAAAAAAAAAAAA'
        'AAAAAAA9dWR0YQAAADVtZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAAhpbHN0AAAAgG1vb2YAAAAQ'
        'bWZoZAAAAAAAAAABAAAAaHRyYWYAAAAkdGZoZAAAADkAAAABAAAAAAAAAuIAAAQAAAACzQEBAAAAAAAUdGZkdAEAAAAAAAAAAAAA'
        'AAAAACh0cnVuAAACBQAAAAQAAACIAgAAAAAAAs0AAADGAAAA2AAAANEAAAVEbWRhdAAAAgEGBf/93EXpvebZSLeWLNgg2SPu73gy'
        'NjQgLSBjb3JlIDE2NSAtIEguMjY0L01QRUctNCBBVkMgY29kZWMgLSBDb3B5bGVmdCAyMDAzLTIwMjUgLSBodHRwOi8vd3d3LnZp'
        'ZGVvbGFuLm9yZy94MjY0Lmh0bWwgLSBvcHRpb25zOiBjYWJhYz0xIHJlZj0xNiBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgzOjB4'
        'MTMzIG1lPXVtaCBzdWJtZT05IHBzeT0wIG1peGVkX3JlZj0xIG1lX3JhbmdlPTI0IGNocm9tYV9tZT0xIHRyZWxsaXM9MCA4eDhk'
        'Y3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTAgY2hyb21hX3FwX29mZnNldD0wIHRocmVhZHM9MSBsb29rYWhl'
        'YWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAg'
        'Y29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0yIGtleWludD05OTk5IGtleWludF9taW49MTAgc2NlbmVjdXQ9'
        'NDAgaW50cmFfcmVmcmVzaD0wIHJjPWNxcCBtYnRyZWU9MCBxcD0wAIAAAADEZYiCV9fjwAr/19mQeLwwgDqDJ0lk0ZZuuVrXXNcq'
        '0D1wY9XsuB0oaACPy/XQP3YjzX8xcbL5XnNzqbAxkKr/UQer2lEySeyfxhoAXEF5rfbr8XDU8wG2VPnfAi6BSlvBm+R65kVJtf5d'
        'p4KKAMk5SKTcQ2ZUPUm/eA9wIv5ux/7oj6GnEdoSiVREH1Si1HE3lRsuFLmdKvykWjg6kMieme5O9dBSrhA+L1f5gdMqd47b4stf'
        'p6ABElOCsy/mjnJyZSNb+QAAAMJBiIVf1+PACv/X2ZFgdTrFlBHVPgqNlBJsI1vFWsGwqZe1SWGA7gdKGgbjy0Qphw5Q4ru/+B0m'
        'IfwT0Z+5ljm6a7ir2lK3tp1yyskG2TN5Lc6tHWYspcDU61bg56lkEN+lQ7idf3A6794idQ+Q+vpekh5WDHzeGUlLLi9tXll//u2N'
        'rSV1bIwMQk1lkJvzSe3wE30hhkitu+UyeXTQRQol5wTkZX5exX+YHTKneO2+LLX6egARJTgrMv5o5ycmUjW/gAAAANRBiIlf1+PA'
        'fxviBQujPeAPillTWap75ML6WdUYqzl61ZXvHtbI04GCcqfBOiLkgdx35fGM9AVSrdIDJ+zlOZVbxoXtKJkk+2BTeOcbH8gLT4mX'
        '7TQikUx6J0MBe2kS23hI/iZlmiC9yxPSQdTq7kpLb3qu/y+m/pY+t76kJMvDRI5Ex+xnC/lz9eVJohv/1zMqMtFYGaCHF1GnFxd0'
        'mLzGjMLZtKoeq+6l85YocpgAYIzZdxL+d0kLHozK/uFchBOxFuB0kw6n3+n9TTs2sU0iSXL/8QAAAM1BiI1f1+PAfxviBQtRS0Fk'
        '5UWUaFO30rHoAYMNGX/DsxBotVYl0fmlst46OyBCi8TI/z+GbRZH4riRL3ap87kEfLpfmU2K17DqXcbH77EpNZ15zl2DTHszxSDg'
        '4J+KPdCjh7E0y0fTFC0LYev8uqPhCc/Iy9bse2smWsPVbXaPJ2O2l9j/uZqU3NFJfHSdVVaywL1f1PfgSTnib6WQOsbRyUhTHCrB'
        '5AFOFwFHmLZhXvetXTt6NtAN66/zA6ZU7x23xZa/T0ACJKcFdf+AAAAAQ21mcmEAAAArdGZyYQEAAAAAAAABAAAAAAAAAAEAAAAA'
        'AAAAAAAAAAAAAALiAQEBAAAAEG1mcm8AAAAAAAAAQw=='
    )),
    ('fixtures/b1-small-loop.gif', 'gif', 'e9e51ceb101da4c184c88b6ee9fe84aebedb0dd57371631907cc2edfd2b67ab9', base64.b64decode(
        'R0lGODlhAgACAIcAAAAAAB01YTpqwlefI3TUhJEJ5a4+Rstzp+ioCAXdaSISyj9HK1x8jHmx7ZbmTrMbr9BQEO2FcQq60ifvM0Qk'
        'lGFZ9X6OVpvDt7j4GNUtefJi2g+XOyzMnEkB/WY2XoNrv6CgIL3VgdoK4vc/QxR0pDGpBU7eZmsTx4hIKKV9icKy6t/nS/wcrBlR'
        'DTaGblO7z3DwMI0lkapa8sePU+TEtAH5FR4udjtj11iYOHXNmZIC+q83W8xsvOmhHQbWfiML30BAQF11oXqqApffY7QUxNFJJe5+'
        'hguz5yjoSEUdqWJSCn+Ha5y8zLnxLdYmjvNb7xCQUC3FsUr6Emcvc4Rk1KGZNb7OltsD9/g4WBVtuTKiGk/Xe2wM3IlBPaZ2nsOr'
        '/+DgYP0VwRpKIjd/g1S05HHpRY4epqtTB8iIaOW9yQLyKh8nizxc7FmRTXbGrpP7D7AwcM1l0eqaMgfPkyQE9EE5VV5utnujF5jY'
        'eLUN2dJCOu93mwys/CnhXUYWvmNLH4CAgJ214brqQtcfo/RUBBGJZS6+xkvzJ2goiIVd6aKSSr/Hq9z8DPkxbRZmzjObL1DQkG0F'
        '8Yo6Uqdvs8SkFOHZdf4O1htDNzh4mFWt+XLiWo8Xu6xMHMmBfea23gPrPyAgoD1VAVqKYne/w5T0JLEphc5e5uuTRwjIqCX9CUIy'
        'al9ny3ycLJnRjbYG7tM7T/BwsA2lESrackcP02RENIF5lZ6u9rvjV9gYuPVNGRKCei+320zsPGkhnYZW/qOLX8DAwN31Ifoqghdf'
        '4zSURFHJpW7+BoszZ6hoyMWdKeLSiv8H6xw8TDlxrVamDnPbb5AQ0K1FMcp6kuev8wTkVCEZtT5OFluDd3i42JXtObIims9X++yM'
        'XAnBvSb2HkMrf2Bg4H2VQZrKorf/A9Q0ZPFpxQ6eJivTh0gI6GU9SYJyqp+nC7zcbNkRzfZGLhN7jzCw8E3lUWoasodPE6SEdMG5'
        '1d7uNvsjlxhY+DWNWVLCum/3G4wsfKlh3caWPuPLnyH/C05FVFNDQVBFMi4wAwEAAAAh+QQIBAAAACwAAAAAAgACAAAIBwABBBAw'
        'ICAAIfkECAwAAAAsAAAAAAIAAgCHAAAAHTVhOmrCV58jdNSEkQnlrj5Gy3On6KgIBd1pIhLKP0crXHyMebHtluZOsxuv0FAQ7YVx'
        'CrrSJ+8zRCSUYVn1fo5Wm8O3uPgY1S158mLaD5c7LMycSQH9ZjZeg2u/oKAgvdWB2gri9z9DFHSkMakFTt5maxPHiEgopX2JwrLq'
        '3+dL/BysGVENNoZuU7vPcPAwjSWRqlryx49T5MS0AfkVHi52O2PXWJg4dc2ZkgL6rzdbzGy86aEdBtZ+IwvfQEBAXXWheqoCl99j'
        'tBTE0Ukl7n6GC7PnKOhIRR2pYlIKf4drnLzMufEt1iaO81vvEJBQLcWxSvoSZy9zhGTUoZk1vs6W2wP3+DhYFW25MqIaT9d7bAzc'
        'iUE9pnaew6v/4OBg/RXBGkoiN3+DVLTkcelFjh6mq1MHyIho5b3JAvIqHyeLPFzsWZFNdsauk/sPsDBwzWXR6poyB8+TJAT0QTlV'
        'Xm62e6MXmNh4tQ3Z0kI673ebDKz8KeFdRha+Y0sfgICAnbXhuupC1x+j9FQEEYllLr7GS/MnaCiIhV3popJKv8er3PwM+TFtFmbO'
        'M5svUNCQbQXxijpSp2+zxKQU4dl1/g7WG0M3OHiYVa35cuJajxe7rEwcyYF95rbeA+s/ICCgPVUBWopid7/DlPQksSmFzl7m65NH'
        'CMioJf0JQjJqX2fLfJwsmdGNtgbu0ztP8HCwDaURKtpyRw/TZEQ0gXmVnq72u+NX2Bi49U0ZEoJ6L7fbTOw8aSGdhlb+o4tfwMDA'
        '3fUh+iqCF1/jNJREUcmlbv4GizNnqGjIxZ0p4tKK/wfrHDxMOXGtVqYOc9tvkBDQrUUxynqS56/zBORUIRm1Pk4WW4N3eLjYle05'
        'siKaz1f77IxcCcG9JvYeQyt/YGDgfZVBmsqit/8D1DRk8WnFDp4mK9OHSAjoZT1JgnKqn6cLvNxs2RHN9kYuE3uPMLDwTeVRahqy'
        'h08TpIR0wbnV3u42+yOXGFj4NY1ZUsK6b/cbjCx8qWHdxpY+48ufCAcAhxApYiQgACH5BAgYAAAALAAAAAACAAIAhwAAAB01YTpq'
        'wlefI3TUhJEJ5a4+Rstzp+ioCAXdaSISyj9HK1x8jHmx7ZbmTrMbr9BQEO2FcQq60ifvM0QklGFZ9X6OVpvDt7j4GNUtefJi2g+X'
        'OyzMnEkB/WY2XoNrv6CgIL3VgdoK4vc/QxR0pDGpBU7eZmsTx4hIKKV9icKy6t/nS/wcrBlRDTaGblO7z3DwMI0lkapa8sePU+TE'
        'tAH5FR4udjtj11iYOHXNmZIC+q83W8xsvOmhHQbWfiML30BAQF11oXqqApffY7QUxNFJJe5+hguz5yjoSEUdqWJSCn+Ha5y8zLnx'
        'LdYmjvNb7xCQUC3FsUr6Emcvc4Rk1KGZNb7OltsD9/g4WBVtuTKiGk/Xe2wM3IlBPaZ2nsOr/+DgYP0VwRpKIjd/g1S05HHpRY4e'
        'pqtTB8iIaOW9yQLyKh8nizxc7FmRTXbGrpP7D7AwcM1l0eqaMgfPkyQE9EE5VV5utnujF5jYeLUN2dJCOu93mwys/CnhXUYWvmNL'
        'H4CAgJ214brqQtcfo/RUBBGJZS6+xkvzJ2goiIVd6aKSSr/Hq9z8DPkxbRZmzjObL1DQkG0F8Yo6Uqdvs8SkFOHZdf4O1htDNzh4'
        'mFWt+XLiWo8Xu6xMHMmBfea23gPrPyAgoD1VAVqKYne/w5T0JLEphc5e5uuTRwjIqCX9CUIyal9ny3ycLJnRjbYG7tM7T/BwsA2l'
        'ESrackcP02RENIF5lZ6u9rvjV9gYuPVNGRKCei+320zsPGkhnYZW/qOLX8DAwN31Ifoqghdf4zSURFHJpW7+BoszZ6hoyMWdKeLS'
        'iv8H6xw8TDlxrVamDnPbb5AQ0K1FMcp6kuev8wTkVCEZtT5OFluDd3i42JXtObIims9X++yMXAnBvSb2HkMrf2Bg4H2VQZrKorf/'
        'A9Q0ZPFpxQ6eJivTh0gI6GU9SYJyqp+nC7zcbNkRzfZGLhN7jzCw8E3lUWoasodPE6SEdMG51d7uNvsjlxhY+DWNWVLCum/3G4ws'
        'fKlh3caWPuPLnwgHAA0dQpQoIAA7'
    )),
    ('fixtures/b1-small.mp4', 'mp4', '6ee2813bb3ec914898e3c710f189fe8f73cb29cbcbff3e91eb01e4758ff858eb', base64.b64decode(
        'AAAAJGZ0eXBpc29tAAACAGlzb21pc282aXNvMmF2YzFtcDQxAAACvW1vb3YAAABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAAAAAEA'
        'AAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
        'AAIAAAHkdHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAA'
        'AQAAAAAAAAAAAAAAAAAAQAAAAAADAAAAAwAAAAABgG1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAMgAAAAAAVcQAAAAAAC1oZGxy'
        'AAAAAAAAAAB2aWRlAAAAAAAAAAAAAAAAVmlkZW9IYW5kbGVyAAAAASttaW5mAAAAFHZtaGQAAAABAAAAAAAAAAAAAAAkZGluZgAA'
        'ABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAADrc3RibAAAAJ9zdHNkAAAAAAAAAAEAAACPYXZjMQAAAAAAAAABAAAAAAAAAAAA'
        'AAAAAAAAAAADAAMASAAAAEgAAAAAAAAAAQ9MYXZjIGxpYngyNjRyZ2IAAAAAAAAAAAAAAAAAAAAAABj//wAAADlhdmNDAfQACv/h'
        'ABxn9AAKkcmEX46Om4EBACAAAAMAIAAABkHiRMIwAQAGaOhDAa8s//j4AAAAABBzdHRzAAAAAAAAAAAAAAAQc3RzYwAAAAAAAAAA'
        'AAAAFHN0c3oAAAAAAAAAAAAAAAAAAAAQc3RjbwAAAAAAAAAAAAAAKG12ZXgAAAAgdHJleAAAAAAAAAABAAAAAQAAAAAAAAAAAAAA'
        'AAAAAD11ZHRhAAAANW1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAACGlsc3QAAAB8bW9vZgAAABBt'
        'ZmhkAAAAAAAAAAEAAABkdHJhZgAAACR0ZmhkAAAAOQAAAAEAAAAAAAAC4QAAAgAAAAJoAQEAAAAAABR0ZmR0AQAAAAAAAAAAAAAA'
        'AAAAJHRydW4AAAIFAAAAAwAAAIQCAAAAAAACaAAAAE8AAABnAAADJm1kYXQAAAIBBgX//dxF6b3m2Ui3lizYINkj7u94MjY0IC0g'
        'Y29yZSAxNjUgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDI1IC0gaHR0cDovL3d3dy52aWRlb2xh'
        'bi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MTYgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDEzMyBt'
        'ZT11bWggc3VibWU9OSBwc3k9MCBtaXhlZF9yZWY9MSBtZV9yYW5nZT0yNCBjaHJvbWFfbWU9MSB0cmVsbGlzPTAgOHg4ZGN0PTEg'
        'Y3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0wIGNocm9tYV9xcF9vZmZzZXQ9MCB0aHJlYWRzPTEgbG9va2FoZWFkX3Ro'
        'cmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0'
        'cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0wIHdlaWdodHA9MiBrZXlpbnQ9OTk5OSBrZXlpbnRfbWluPTI1IHNjZW5lY3V0PTQwIGlu'
        'dHJhX3JlZnJlc2g9MCByYz1jcXAgbWJ0cmVlPTAgcXA9MACAAAAAX2WIglfYX2pQp8a4Ux4HUHCbllrkGwczTdLxpfVfcllOFrow'
        'g1bWSCKQ0TaZT+VzbI38L2V6PAwRo5ru2gfH3N5K7gL1LBfvvWjIwJtUBho5wXIEjq3bYZuBLT5LGH/9AAAAS0GIhV/YX2pQp8a3'
        '+Ndl7mYNnZQEIE4aGEnGsdLbyUwqtwB6fm8lKJj6C4N7qT82inB0EN+lQ7oeFrDsh86M8umgXEvOCcjK/L9//gAAAGNBiIlf2F9q'
        'UKfGt/lK2nQwvYqenKJeVH4uFAphOe2Spqpjnx4ObyXZNHF8Z1OkMTD3HVXD+CzcXMeXHo6GLYH0IRfYexdZEil4JtKnyJ8lb85Y'
        'ocpgAYItndxL+d0kLHo///EAAABDbWZyYQAAACt0ZnJhAQAAAAAAAAEAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAuEBAQEAAAAQbWZy'
        'bwAAAAAAAABD'
    )),
)


def _canonical_fixtures():
    fixtures = []
    for path, kind, expected_sha256, data in _FIXTURE_SPECS:
        if _sha(data) != expected_sha256:
            raise RuntimeError("embedded fixture digest mismatch")
        fixtures.append((path, kind, data))
    return fixtures

class InjectedCrash(RuntimeError):
    """Deterministic test-only interruption raised at a durable boundary."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_json(raw: bytes, description: str):
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{description} is not valid duplicate-safe JSON") from error
    if raw != canonical_json(value):
        raise ValueError(f"{description} is not canonical JSON")
    return value


def _safe_relative(value: object) -> str:
    if type(value) is not str or not value or value != unicodedata.normalize("NFC", value):
        raise ValueError("manifest path is empty or non-NFC")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.endswith("/") or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError("manifest path is unsafe")
    if "\\" in value or "\x00" in value:
        raise ValueError("manifest path is unsafe")
    return value


def _absolute(value: str, name: str, *, must_exist: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be an absolute normalized path")
    if must_exist and not path.exists():
        raise ValueError(f"{name} does not exist")
    return path


def _read_regular(path: Path, *, max_bytes: int = 256 * 1024 * 1024):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"unsafe regular file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"file identity changed: {path}")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"file exceeds bound: {path}")
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError(f"file changed while read: {path}")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _read_descriptor(fd: int, *, max_bytes: int = 256 * 1024 * 1024):
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("unsafe regular file descriptor")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("file descriptor exceeds bound")
    after = os.fstat(fd)
    if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("file descriptor changed while read")
    return b"".join(chunks), after


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)


def _open_parent_at(root_fd: int, relative: str) -> tuple[int, str]:
    parts = PurePosixPath(_safe_relative(relative)).parts
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = following
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _read_at(parent_fd: int, name: str, *, allow_absent: bool = False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent_fd)
    except FileNotFoundError:
        if allow_absent:
            return None
        raise
    try:
        return _read_descriptor(fd)
    finally:
        os.close(fd)


def _unlink_exact_candidate_at(parent_fd: int, name: str, release_entry: dict,
                               owner: dict, *, remove: bool = True):
    """Validate an absent-baseline path and optionally unlink the exact candidate."""
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError("rollback unlink target is unsafe")
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ValueError("rollback unlink target is unsafe") from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("rollback unlink target is unsafe")
        try:
            data, verified = _read_descriptor(descriptor)
        except ValueError as error:
            raise ValueError("rollback unlink target is unsafe") from error
        expected = (
            release_entry["sha256"], release_entry["size"], release_entry["mode"],
            owner["uid"], owner["gid"],
        )
        observed = (
            _sha(data), len(data), f"{stat.S_IMODE(verified.st_mode):04o}",
            verified.st_uid, verified.st_gid,
        )
        if observed != expected:
            raise ValueError("rollback unlink target is unsafe")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        pinned_metadata = (
            verified.st_dev, verified.st_ino, verified.st_uid, verified.st_gid,
            verified.st_mode, verified.st_nlink, verified.st_size,
            verified.st_mtime_ns, verified.st_ctime_ns,
        )
        current_metadata = (
            current.st_dev, current.st_ino, current.st_uid, current.st_gid,
            current.st_mode, current.st_nlink, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns,
        )
        if current_metadata != pinned_metadata:
            raise ValueError("rollback unlink target is unsafe")
        if remove:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    finally:
        os.close(descriptor)


def _atomic_write_at(parent_fd: int, name: str, data: bytes, mode: int, *, uid: int, gid: int):
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                 mode, dir_fd=parent_fd)
    published = False
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short durable write")
            view = view[count:]
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        published = True
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if not published:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _same_namespace_inode(fd: int, path: Path) -> bool:
    opened = os.fstat(fd)
    current = path.lstat()
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


def _fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_noreplace(source: str, destination: str, *, parent_fd: int):
    """Atomically publish one sibling without ever replacing an existing name."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as error:
        raise OSError(errno.ENOSYS, "renameat2 RENAME_NOREPLACE is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(parent_fd, os.fsencode(source), parent_fd,
                 os.fsencode(destination), 1) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _fresh_sibling_name(path: Path) -> str:
    return f".{path.name}.{os.getpid()}.{secrets.token_hex(16)}"


def _atomic_write(path: Path, data: bytes, mode: int, *, uid=None, gid=None):
    parent = path.parent
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"unsafe publication parent: {parent}")
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, mode)
    published = False
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short durable write")
            view = view[count:]
        os.fchmod(fd, mode)
        os.fchown(fd, os.geteuid() if uid is None else uid, os.getegid() if gid is None else gid)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        published = True
        _fsync_dir(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if not published:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _durable_new_file(path: Path, data: bytes, mode: int):
    """Create one immutable stage/backup file with durable bytes and metadata."""
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short durable file write")
            view = view[count:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _contained_path(root: Path, relative: str) -> Path:
    """Resolve a manifest path without permitting an intermediate symlink."""
    parts = PurePosixPath(_safe_relative(relative)).parts
    current = root
    for part in parts[:-1]:
        current = current / part
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("manifest parent path is unsafe")
    target = current / parts[-1]
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("manifest target path is unsafe")
    return target


def _state_digest(state: dict) -> str:
    unsigned = dict(state)
    unsigned["self"] = dict(state["self"])
    unsigned["self"]["sha256"] = ""
    return _sha(canonical_json(unsigned))


def _populate_state_fd(path: Path, state: dict, fd: int):
    """Populate and sync the locked state inode before it becomes visible."""
    metadata = os.fstat(fd)
    state["self"] = {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid, "mode": "0600", "sha256": "",
    }
    state["self"]["sha256"] = _state_digest(state)
    raw = canonical_json(state)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(raw)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short durable state write")
        view = view[count:]
    os.fsync(fd)


def _mkdir_fresh(path: Path, mode: int = 0o700):
    if path.exists() or path.is_symlink():
        raise ValueError(f"path must be fresh: {path}")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"unsafe parent: {parent}")
    path.mkdir(mode=mode)
    os.chmod(path, mode)
    _fsync_dir(parent)


def _git(repository: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        [GIT, "-C", str(repository), *args], check=True, capture_output=True,
        text=not binary, timeout=30, shell=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0"},
    )
    return result.stdout


def _commit(repository: Path, value: str) -> tuple[str, str]:
    if SHA1.fullmatch(value) is None:
        raise ValueError("commit identity must be lowercase full SHA")
    commit = _git(repository, "rev-parse", "--verify", f"{value}^{{commit}}").strip()
    if commit != value:
        raise ValueError("requested identity is not an exact commit")
    tree = _git(repository, "rev-parse", "--verify", f"{value}^{{tree}}").strip()
    if SHA1.fullmatch(tree) is None:
        raise ValueError("commit tree identity is invalid")
    return commit, tree


def _approved_baseline_snapshot(destination: Path, release: dict, baseline_sha: str,
                                baseline_tree_sha: str, owner: dict) -> list[dict]:
    """Prove changed predecessors from Git objects, then capture matching live bytes."""
    git_dir = destination / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise ValueError("destination must be a preserved Git checkout")
    commit, tree = _commit(destination, baseline_sha)
    if (commit, tree) != (baseline_sha, baseline_tree_sha):
        raise ValueError("destination Git object database lacks the approved baseline tree")
    if _git(destination, "rev-parse", "--verify", "HEAD^{commit}").strip() != baseline_sha:
        raise ValueError("destination checkout is not at the approved baseline commit")
    root_fd = _open_directory(destination)
    records = []
    try:
        for entry in release["files"]:
            relative = entry["path"]
            line = _git(destination, "ls-tree", baseline_sha, "--", relative).strip()
            parent_fd, name = _open_parent_at(root_fd, relative)
            try:
                live = _read_at(parent_fd, name, allow_absent=True)
            finally:
                os.close(parent_fd)
            if not line:
                if live is not None:
                    raise ValueError("live predecessor does not equal approved baseline")
                records.append({"path": relative, "present": False, "sha256": None,
                                "size": 0, "mode": None, "data": None})
                continue
            mode_value, object_type, object_sha, object_path = line.split(None, 3)
            if object_type != "blob" or object_path != relative or mode_value not in ("100644", "100755"):
                raise ValueError("approved baseline path object is unsupported")
            expected_data = _git(destination, "cat-file", "blob", object_sha, binary=True)
            expected_mode = "0755" if mode_value == "100755" else "0644"
            if live is None:
                raise ValueError("live predecessor does not equal approved baseline")
            data, metadata = live
            if (data, f"{stat.S_IMODE(metadata.st_mode):04o}", metadata.st_uid, metadata.st_gid) != (
                    expected_data, expected_mode, owner["uid"], owner["gid"]):
                raise ValueError("live predecessor does not equal approved baseline")
            records.append({"path": relative, "present": True, "sha256": _sha(data),
                            "size": len(data), "mode": expected_mode, "data": data})
    finally:
        os.close(root_fd)
    return records


def _manifest_file(path: Path, expected_path: str | None = None):
    data, metadata = _read_regular(path)
    if expected_path is not None and path.as_posix() != expected_path:
        raise ValueError("manifest canonical path mismatch")
    return {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}", "sha256": _sha(data),
    }


def _directory_authority(path: Path):
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"unsafe authority directory: {path}")
    return {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _validate_directory_authority(record: object, description: str) -> Path:
    keys = {"path", "dev", "ino", "uid", "gid", "mode"}
    if type(record) is not dict or set(record) != keys:
        raise ValueError(f"{description} identity schema is invalid")
    path = _absolute(record["path"], description, must_exist=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{description} identity is unsafe")
    if any(type(record[key]) is not int for key in ("dev", "ino", "uid", "gid")) or (
        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
        f"{stat.S_IMODE(metadata.st_mode):04o}",
    ) != (record["dev"], record["ino"], record["uid"], record["gid"], record["mode"]):
        raise ValueError(f"{description} identity mismatch")
    return path


def _validate_file_authority(record: object) -> tuple[bytes, os.stat_result]:
    keys = {"path", "dev", "ino", "uid", "gid", "mode", "sha256"}
    if type(record) is not dict or set(record) != keys:
        raise ValueError("file authority schema is invalid")
    path = _absolute(record["path"], "authority file", must_exist=True)
    data, metadata = _read_regular(path)
    if any(type(record[key]) is not int for key in ("dev", "ino", "uid", "gid")):
        raise ValueError("file authority metadata is invalid")
    if (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
            f"{stat.S_IMODE(metadata.st_mode):04o}", _sha(data)) != (
            record["dev"], record["ino"], record["uid"], record["gid"], record["mode"], record["sha256"]):
        raise ValueError("file authority identity or digest mismatch")
    return data, metadata


def _validate_release_manifest(raw: bytes, release_root: Path, *, expected_boundary="b1"):
    value = _parse_json(raw, "release manifest")
    keys = {"version", "boundary", "baseline_sha", "source_sha", "tree_sha",
            "reviewed_diff_sha256", "fixture_manifest_sha256", "files"}
    if type(value) is not dict or set(value) != keys or value["version"] != 1 or value["boundary"] != expected_boundary:
        raise ValueError("release manifest schema is invalid")
    for key in ("baseline_sha", "source_sha", "tree_sha"):
        if type(value[key]) is not str or SHA1.fullmatch(value[key]) is None:
            raise ValueError("release Git identity is invalid")
    for key in ("reviewed_diff_sha256", "fixture_manifest_sha256"):
        if type(value[key]) is not str or SHA256.fullmatch(value[key]) is None:
            raise ValueError("release digest is invalid")
    files = value["files"]
    if type(files) is not list or not files:
        raise ValueError("release files must be nonempty")
    names = []
    for entry in files:
        if type(entry) is not dict or set(entry) != {"mode", "path", "sha256", "size"}:
            raise ValueError("release file entry schema is invalid")
        relative = _safe_relative(entry["path"])
        if relative.startswith(FORBIDDEN_PREFIXES):
            raise ValueError("mutable or private path is forbidden from source release")
        if entry["mode"] not in ("0644", "0755") or type(entry["size"]) is not int or entry["size"] < 0:
            raise ValueError("release file metadata is invalid")
        if type(entry["sha256"]) is not str or SHA256.fullmatch(entry["sha256"]) is None:
            raise ValueError("release file digest is invalid")
        path = _contained_path(release_root, relative)
        data, metadata = _read_regular(path)
        if (len(data), _sha(data), f"{stat.S_IMODE(metadata.st_mode):04o}") != (
                entry["size"], entry["sha256"], entry["mode"]):
            raise ValueError("release file bytes or metadata mismatch")
        names.append(relative)
    if names != sorted(set(names)):
        raise ValueError("release paths are unordered or duplicate")
    actual = []
    for path in sorted(release_root.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("release tree contains unsafe object")
        if stat.S_ISREG(info.st_mode):
            actual.append(path.relative_to(release_root).as_posix())
    if actual != names:
        raise ValueError("release tree has missing or extra files")
    return value


def _decoded_fixture_metadata(path: Path, kind: str) -> tuple[int, int, int, list[int]]:
    if kind == "gif":
        try:
            with Image.open(path) as image:
                fallback = image.info.get("duration", 100)
                durations = []
                for frame in ImageSequence.Iterator(image):
                    duration = frame.info.get("duration", fallback)
                    if type(duration) is not int:
                        raise ValueError("GIF fixture timeline mismatch")
                    durations.append(duration)
                return image.width, image.height, len(durations), durations
        except (OSError, ValueError) as error:
            raise ValueError("GIF fixture decoder mismatch") from error
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,nb_frames,avg_frame_rate,duration:frame=duration_time",
         "-of", "json", str(path)], check=False, capture_output=True, text=True,
        timeout=15, shell=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise ValueError("MP4 fixture decoder mismatch")
    try:
        probe = json.loads(result.stdout, object_pairs_hook=_unique)
        streams = probe["streams"]
        frames = probe["frames"]
        if type(streams) is not list or len(streams) != 1 or type(frames) is not list or not frames:
            raise ValueError
        stream = streams[0]
        width, height = stream["width"], stream["height"]
        fallback = Fraction(1, 1) / Fraction(stream["avg_frame_rate"])
        durations = []
        for frame in frames:
            seconds = Fraction(frame.get("duration_time")) if frame.get("duration_time") else fallback
            milliseconds = seconds * 1000
            if milliseconds.denominator != 1:
                raise ValueError
            durations.append(milliseconds.numerator)
        if "nb_frames" in stream and int(stream["nb_frames"]) != len(frames):
            raise ValueError
        if type(width) is not int or type(height) is not int:
            raise ValueError
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError("MP4 fixture decoder mismatch") from error
    return width, height, len(frames), durations


def _validate_fixture_manifest(raw: bytes, root: Path):
    value = _parse_json(raw, "fixture manifest")
    if type(value) is not dict or set(value) != {"version", "boundary", "source_sha", "tree_sha", "fixtures"}:
        raise ValueError("fixture manifest schema is invalid")
    if value["version"] != 1 or value["boundary"] != "b1":
        raise ValueError("fixture manifest boundary is invalid")
    fixtures = value["fixtures"]
    if type(fixtures) is not list or not fixtures:
        raise ValueError("fixture list is invalid")
    names = []
    for entry in fixtures:
        keys = {"path", "sha256", "size", "kind", "width", "height", "frames", "durations_ms"}
        if type(entry) is not dict or set(entry) != keys:
            raise ValueError("fixture entry schema is invalid")
        relative = _safe_relative(entry["path"])
        if not relative.startswith("fixtures/") or entry["kind"] not in ("gif", "mp4"):
            raise ValueError("fixture path or kind is invalid")
        integers = (entry["size"], entry["width"], entry["height"], entry["frames"])
        if any(type(item) is not int for item in integers) or entry["size"] < 0 or any(item <= 0 for item in integers[1:]):
            raise ValueError("fixture dimensions are invalid")
        durations = entry["durations_ms"]
        if type(durations) is not list or len(durations) != entry["frames"] or any(
                type(item) is not int or not 20 <= item <= 10000 for item in durations):
            raise ValueError("fixture timeline is invalid")
        fixture_path = _contained_path(root, relative)
        data, _ = _read_regular(fixture_path)
        if len(data) != entry["size"] or _sha(data) != entry["sha256"]:
            raise ValueError("fixture bytes mismatch")
        actual = _decoded_fixture_metadata(fixture_path, entry["kind"])
        if actual != (entry["width"], entry["height"], entry["frames"], durations):
            raise ValueError("fixture metadata mismatch")
        names.append(relative)
    if names != sorted(set(names)):
        raise ValueError("fixture paths are unordered or duplicate")
    return value


def build_b1(arguments) -> dict[str, object]:
    repository = _absolute(arguments.repository, "repository", must_exist=True)
    if not repository.is_dir() or repository.is_symlink():
        raise ValueError("repository is unsafe")
    baseline, _baseline_tree = _commit(repository, arguments.baseline_commit)
    source, tree = _commit(repository, arguments.source_commit)
    if baseline == source:
        raise ValueError("baseline and source commits must differ")
    if SHA256.fullmatch(arguments.expected_reviewed_diff_sha256 or "") is None:
        raise ValueError("reviewed diff digest is invalid")
    python = _absolute(arguments.python, "python", must_exist=True)
    py_target = python.resolve(strict=True)
    py_info = py_target.lstat()
    if not stat.S_ISREG(py_info.st_mode) or not os.access(python, os.X_OK):
        raise ValueError("python interpreter is unsafe")
    stage = _absolute(arguments.stage_root, "stage root")
    created = False
    try:
        _mkdir_fresh(stage)
        created = True
        release = stage / "release"
        release.mkdir(mode=0o700)
        fixtures_root = stage / "fixtures"
        fixtures_root.mkdir(mode=0o700)
        diff = _git(repository, "diff", "--binary", "--full-index", "--no-ext-diff",
                    f"{baseline}...{source}", binary=True)
        if _sha(diff) != arguments.expected_reviewed_diff_sha256:
            raise ValueError("reviewed diff SHA-256 mismatch")
        raw_names = _git(repository, "diff", "--name-status", "-z", "--no-ext-diff",
                         f"{baseline}...{source}", binary=True)
        pieces = raw_names.split(b"\0")
        if pieces and pieces[-1] == b"":
            pieces.pop()
        changed = []
        index = 0
        while index < len(pieces):
            status_code = pieces[index].decode("ascii")
            index += 1
            if status_code not in ("A", "M") or index >= len(pieces):
                raise ValueError("B1 source diff contains a deletion, rename, or unsupported change")
            relative = _safe_relative(pieces[index].decode("utf-8"))
            index += 1
            if relative.startswith(FORBIDDEN_PREFIXES):
                raise ValueError("B1 source diff contains mutable/private paths")
            changed.append(relative)
        if changed != sorted(set(changed)) or not changed:
            raise ValueError("changed-file allowlist is empty, duplicate, or unordered")
        records = []
        for relative in changed:
            data = _git(repository, "show", f"{source}:{relative}", binary=True)
            tree_line = _git(repository, "ls-tree", source, "--", relative).strip()
            if not tree_line:
                raise ValueError("changed path is absent from source tree")
            mode_value, object_type, _rest = tree_line.split(None, 2)
            if object_type != "blob" or mode_value not in ("100644", "100755"):
                raise ValueError("changed Git object mode/type is unsupported")
            mode = "0755" if mode_value == "100755" else "0644"
            destination = release.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _durable_new_file(destination, data, int(mode, 8))
            records.append({"mode": mode, "path": relative, "sha256": _sha(data), "size": len(data)})
        fixture_specs = _canonical_fixtures()
        fixture_records = []
        for relative, kind, data in fixture_specs:
            path = stage.joinpath(*PurePosixPath(relative).parts)
            _durable_new_file(path, data, 0o644)
            width, height, frames, durations = _decoded_fixture_metadata(path, kind)
            fixture_records.append({
                "path": relative, "sha256": _sha(data), "size": len(data), "kind": kind,
                "width": width, "height": height, "frames": frames,
                "durations_ms": durations,
            })
        fixture_manifest = {"version": 1, "boundary": "b1", "source_sha": source,
                            "tree_sha": tree, "fixtures": fixture_records}
        fixture_bytes = canonical_json(fixture_manifest)
        _atomic_write(stage / "physical-fixtures.json", fixture_bytes, 0o600)
        release_manifest = {
            "version": 1, "boundary": "b1", "baseline_sha": baseline, "source_sha": source,
            "tree_sha": tree, "reviewed_diff_sha256": _sha(diff),
            "fixture_manifest_sha256": _sha(fixture_bytes), "files": records,
        }
        release_bytes = canonical_json(release_manifest)
        _atomic_write(stage / "release-manifest.json", release_bytes, 0o600)
        for directory in sorted((path for path in stage.rglob("*") if path.is_dir()), reverse=True):
            _fsync_dir(directory)
        for path in (stage / "release-manifest.json", stage / "physical-fixtures.json"):
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(stage)
        return {
            "boundary": "b1", "diff_sha256": _sha(diff), "file_count": len(records),
            "fixture_count": len(fixture_records), "fixture_manifest": str(stage / "physical-fixtures.json"),
            "fixture_manifest_sha256": _sha(fixture_bytes), "release_manifest": str(stage / "release-manifest.json"),
            "release_manifest_sha256": _sha(release_bytes), "source_sha": source,
            "stage_root": str(stage), "tree_sha": tree,
        }
    except BaseException:
        if created:
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _remaining_service_timeout(deadline, cap):
    if deadline is None:
        return cap
    if type(deadline) not in (int, float) or deadline != deadline:
        raise ValueError("service observation deadline is invalid")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("service observation deadline expired")
    return min(cap, remaining)


class SystemdService:
    def __init__(self, service: str):
        if SERVICE.fullmatch(service) is None:
            raise ValueError("service name is invalid")
        self.name = service

    def _run(self, *args, check=True, deadline=None):
        return subprocess.run(
            [SYSTEMCTL, *args, self.name], check=check, capture_output=True, text=True,
            timeout=_remaining_service_timeout(deadline, 15), shell=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def is_active(self):
        result = self._run("is-active", "--quiet", check=False)
        if result.returncode not in (0, 3):
            raise RuntimeError("service active-state query failed")
        return result.returncode == 0

    def stop(self):
        self._run("stop")
        if self.is_active():
            raise RuntimeError("service did not become inactive")

    def start_ready(self):
        self._run("start")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.is_active():
                connection = http.client.HTTPConnection("127.0.0.1", 5000, timeout=2)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    response.read()
                    if response.status == 302:
                        return
                except OSError:
                    pass
                finally:
                    connection.close()
            time.sleep(0.1)
        raise RuntimeError("bounded service readiness failed")

    def status(self, *, warning_since=None, deadline=None):
        properties_result = self._run(
            "show",
            "--property=Id,ActiveState,SubState,MainPID,NRestarts,ControlGroup",
            deadline=deadline,
        )
        properties = {}
        for line in properties_result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in properties:
                raise RuntimeError("service status properties are malformed")
            properties[key] = value
        if set(properties) != {
            "Id", "ActiveState", "SubState", "MainPID", "NRestarts", "ControlGroup",
        }:
            raise RuntimeError("service status properties are incomplete")
        try:
            main_pid = int(properties["MainPID"])
            n_restarts = int(properties["NRestarts"])
        except ValueError as error:
            raise RuntimeError("service status integer is malformed") from error
        if (
            properties["Id"] != self.name
            or properties["ActiveState"] != "active"
            or properties["SubState"] != "running"
            or main_pid <= 0
            or n_restarts < 0
            or not properties["ControlGroup"].startswith("/")
            or properties["ControlGroup"].split("/")[-1] != self.name
        ):
            raise RuntimeError("service identity or active state is invalid")

        connection = http.client.HTTPConnection(
            "127.0.0.1", 5000, timeout=_remaining_service_timeout(deadline, 2),
        )
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
            status_code = response.status
        finally:
            connection.close()

        lock_raw, lock_metadata = _read_regular(DISPLAY_LOCK, max_bytes=1)
        if lock_raw:
            raise RuntimeError("display lock marker is not empty")
        def display_lock_holder():
            locks_raw, _ = _read_regular(Path("/proc/locks"), max_bytes=1024 * 1024)
            expected_device = f"{os.major(lock_metadata.st_dev):02x}:{os.minor(lock_metadata.st_dev):02x}"
            owner = None
            for line in locks_raw.decode("ascii").splitlines():
                fields = line.split()
                if len(fields) < 6 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                    continue
                device, separator, inode = fields[5].rpartition(":")
                if separator and device.lower() == expected_device and inode == str(lock_metadata.st_ino):
                    try:
                        owner = int(fields[4])
                    except ValueError as error:
                        raise RuntimeError("display lock owner is malformed") from error
                    break
            return owner

        holder_pid = display_lock_holder()
        if holder_pid is None:
            raise RuntimeError("display lock has no owner")
        if holder_pid != main_pid:
            expected_cgroup = f"0::{properties['ControlGroup']}\n".encode("ascii")

            def validate_holder_cgroup():
                try:
                    cgroup_raw, _ = _read_regular(
                        Path(f"/proc/{holder_pid}/cgroup"), max_bytes=64 * 1024,
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    raise RuntimeError("display lock owner cgroup evidence is invalid") from error
                if cgroup_raw != expected_cgroup:
                    raise RuntimeError("display lock is not owned by service cgroup")

            validate_holder_cgroup()
            if display_lock_holder() != holder_pid:
                raise RuntimeError("display lock ownership changed during validation")
            validate_holder_cgroup()

        if display_lock_holder() != holder_pid:
            raise RuntimeError("display lock ownership changed during validation")
        closing_raw, closing_metadata = _read_regular(DISPLAY_LOCK, max_bytes=1)
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")
        if closing_raw or any(
            getattr(closing_metadata, field) != getattr(lock_metadata, field)
            for field in identity_fields
        ):
            raise RuntimeError("display lock path changed during validation")

        warning_count = 0
        if warning_since is not None:
            if type(warning_since) is not int or warning_since < 0:
                raise ValueError("warning baseline is invalid")
            journal = subprocess.run(
                [
                    JOURNALCTL, "--unit", self.name, "--since", f"@{warning_since}",
                    "--priority", "warning", "--output", "cat", "--no-pager",
                ],
                check=True, capture_output=True,
                timeout=_remaining_service_timeout(deadline, 15), shell=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if len(journal.stdout) > 1024 * 1024:
                raise RuntimeError("service warning evidence exceeds bound")
            warning_count = sum(bool(line) for line in journal.stdout.splitlines())

        _remaining_service_timeout(deadline, 15)
        return {
            "service": self.name,
            "active": True,
            "substate": properties["SubState"],
            "http_status": status_code,
            "main_pid": main_pid,
            "n_restarts": n_restarts,
            "control_group": properties["ControlGroup"],
            "display_lock": {
                "path": str(DISPLAY_LOCK), "dev": lock_metadata.st_dev,
                "ino": lock_metadata.st_ino, "uid": lock_metadata.st_uid,
                "gid": lock_metadata.st_gid,
                "mode": f"{stat.S_IMODE(lock_metadata.st_mode):04o}", "locked": True,
            },
            "warning_count": warning_count,
        }


class Priority15Transaction:
    def __init__(self, state_path: Path, state: dict, *, service=None, state_fd: int = -1):
        self.state_path = state_path
        self.state = state
        self._state_fd = state_fd
        self.service = service or SystemdService(state["service"])
        self.release_manifest = None
        self.baseline_manifest = None

    @classmethod
    def load(cls, state_path, *, service=None):
        path = _absolute(str(state_path), "state", must_exist=True)
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("transaction state is locked by another command") from error
            metadata = os.fstat(fd)
            current = path.lstat()
            if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("state identity changed after lock")
            raw, metadata = _read_descriptor(fd)
        except BaseException:
            os.close(fd)
            raise
        if metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("state must be owner-bound mode 0600")
        state = _parse_json(raw, "transaction state")
        if type(state) is not dict or set(state) != STATE_KEYS or state["version"] != 1 or state["boundary"] != "b1":
            raise ValueError("transaction state schema is invalid")
        if state["state_path"] != str(path):
            raise ValueError("transaction state canonical path mismatch")
        identity = state["self"]
        expected_keys = {"path", "dev", "ino", "uid", "gid", "mode", "sha256"}
        if type(identity) is not dict or set(identity) != expected_keys:
            raise ValueError("state self identity schema is invalid")
        if any(type(identity[key]) is not int for key in ("dev", "ino", "uid", "gid")):
            raise ValueError("state self identity metadata is invalid")
        expected = (
            str(path), metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
            f"{stat.S_IMODE(metadata.st_mode):04o}", _state_digest(state),
        )
        actual = (
            identity["path"], identity["dev"], identity["ino"], identity["uid"],
            identity["gid"], identity["mode"], identity["sha256"],
        )
        if actual != expected:
            raise ValueError("state identity mismatch")
        tx = cls(path, state, service=service, state_fd=fd)
        try:
            tx._revalidate()
            return tx
        except BaseException:
            tx.close()
            raise

    def close(self):
        if self._state_fd >= 0:
            os.close(self._state_fd)
            self._state_fd = -1

    def __del__(self):
        self.close()

    def _validate_state_lock(self):
        if self._state_fd < 0:
            raise RuntimeError("transaction state lock is not held")
        metadata = os.fstat(self._state_fd)
        current = self.state_path.lstat()
        if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("state identity changed after lock")

    def _revalidate(self):
        self._validate_state_lock()
        release_raw, _ = _validate_file_authority(self.state["release_manifest"])
        fixture_raw, _ = _validate_file_authority(self.state["fixture_manifest"])
        baseline_raw, _ = _validate_file_authority(self.state["baseline_manifest"])
        release_root = _validate_directory_authority(self.state["release_root"], "release root")
        destination_root = _validate_directory_authority(self.state["destination"], "destination")
        backup = _validate_directory_authority(self.state["backup_root"], "backup root")
        owner = self.state["destination_owner"]
        if (type(owner) is not dict or set(owner) != {"uid", "gid"}
                or any(type(owner[key]) is not int for key in owner)
                or (owner["uid"], owner["gid"]) != (
                    self.state["destination"]["uid"], self.state["destination"]["gid"],
                )):
            raise ValueError("destination owner authority is invalid")
        baseline_service = self.state["service_baseline"]
        if (type(baseline_service) is not dict
                or set(baseline_service) != {"n_restarts", "warning_since"}
                or any(type(baseline_service[key]) is not int for key in baseline_service)
                or min(baseline_service.values()) < 0):
            raise ValueError("service baseline authority is invalid")
        release = _validate_release_manifest(release_raw, release_root)
        fixture = _validate_fixture_manifest(fixture_raw, Path(self.state["fixture_manifest"]["path"]).parent)
        baseline = _parse_json(baseline_raw, "baseline source manifest")
        if release["fixture_manifest_sha256"] != _sha(fixture_raw):
            raise ValueError("fixture manifest digest mismatch")
        if (release["baseline_sha"], release["source_sha"], release["tree_sha"]) != (
                self.state["baseline_sha"], self.state["source_sha"], self.state["tree_sha"]):
            raise ValueError("release/state Git identity mismatch")
        if (fixture["source_sha"], fixture["tree_sha"]) != (self.state["source_sha"], self.state["tree_sha"]):
            raise ValueError("fixture/state Git identity mismatch")
        keys = {"version", "boundary", "source_sha", "tree_sha", "files"}
        if type(baseline) is not dict or set(baseline) != keys or baseline["version"] != 1 or baseline["boundary"] != "b1":
            raise ValueError("baseline manifest schema is invalid")
        if (baseline["source_sha"], baseline["tree_sha"]) != (self.state["baseline_sha"], self.state["baseline_tree_sha"]):
            raise ValueError("baseline manifest identity mismatch")
        if [entry.get("path") for entry in baseline["files"]] != [entry["path"] for entry in release["files"]]:
            raise ValueError("baseline manifest allowlist mismatch")
        for entry in baseline["files"]:
            if type(entry) is not dict or set(entry) != {"path", "present", "sha256", "size", "mode"}:
                raise ValueError("baseline source entry schema is invalid")
            relative = _safe_relative(entry["path"])
            if type(entry["present"]) is not bool or type(entry["size"]) is not int or entry["size"] < 0:
                raise ValueError("baseline source entry metadata is invalid")
            if entry["present"]:
                if entry["mode"] not in ("0644", "0755") or SHA256.fullmatch(entry["sha256"] or "") is None:
                    raise ValueError("baseline source entry is invalid")
                data, metadata = _read_regular(backup / "source" / relative)
                if (len(data), _sha(data), f"{stat.S_IMODE(metadata.st_mode):04o}") != (
                        entry["size"], entry["sha256"], entry["mode"]):
                    raise ValueError("baseline snapshot mismatch")
            elif (entry["sha256"], entry["size"], entry["mode"]) != (None, 0, None):
                raise ValueError("absent baseline source entry is invalid")
        self.release_manifest = release
        self.baseline_manifest = baseline

    @property
    def destination(self):
        return Path(self.state["destination"]["path"])

    @property
    def release_root(self):
        return Path(self.state["release_root"]["path"])

    @property
    def backup_root(self):
        return Path(self.state["backup_root"]["path"])

    def _phase(self, destination_root_fd: int | None = None):
        if self.release_manifest is None or self.baseline_manifest is None:
            raise RuntimeError("transaction manifests were not validated")
        candidate = True
        baseline = True
        owns_root_fd = destination_root_fd is None
        root_fd = _open_directory(self.destination) if owns_root_fd else destination_root_fd
        try:
            for release_entry, baseline_entry in zip(self.release_manifest["files"], self.baseline_manifest["files"]):
                relative = release_entry["path"]
                parent_fd, name = _open_parent_at(root_fd, relative)
                try:
                    live = _read_at(parent_fd, name, allow_absent=True)
                    observed = None if live is None else (
                        _sha(live[0]), len(live[0]), f"{stat.S_IMODE(live[1].st_mode):04o}",
                        live[1].st_uid, live[1].st_gid,
                    )
                    parent_path = self.destination.joinpath(*PurePosixPath(relative).parts[:-1])
                    if not _same_namespace_inode(parent_fd, parent_path):
                        raise ValueError("destination namespace changed")
                finally:
                    os.close(parent_fd)
                owner = self.state["destination_owner"]
                candidate &= observed == (
                    release_entry["sha256"], release_entry["size"], release_entry["mode"],
                    owner["uid"], owner["gid"],
                )
                baseline_expected = None if not baseline_entry["present"] else (
                    baseline_entry["sha256"], baseline_entry["size"], baseline_entry["mode"],
                    owner["uid"], owner["gid"],
                )
                baseline &= observed == baseline_expected
            if not _same_namespace_inode(root_fd, self.destination):
                raise ValueError("destination namespace changed")
        finally:
            if owns_root_fd:
                os.close(root_fd)
        if candidate == baseline:
            raise ValueError("source tree is mixed or ambiguous")
        return "candidate" if candidate else "baseline"

    def _stop(self):
        if self.service.is_active():
            self.service.stop()
        if self.service.is_active():
            raise RuntimeError("service remains active before source mutation")

    def transition(self, target: str, *, operation: str, after_boundary: Callable[[str], None] | None = None):
        if target not in ("candidate", "baseline") or operation not in ("deploy", "rollback", "redeploy", "recover"):
            raise ValueError("transaction target or operation is invalid")
        self._revalidate()
        if self.release_manifest is None or self.baseline_manifest is None:
            raise RuntimeError("transaction manifests were not validated")
        destination_root_fd = _open_directory(self.destination)
        release_root_fd = _open_directory(self.release_root)
        backup_source_fd = _open_directory(self.backup_root / "source")
        pinned = []
        mutation_started = False
        try:
            for release_entry, baseline_entry in zip(self.release_manifest["files"], self.baseline_manifest["files"]):
                relative = release_entry["path"]
                destination_parent, destination_name = _open_parent_at(destination_root_fd, relative)
                source_parent = None
                source_name = None
                if target == "candidate" or baseline_entry["present"]:
                    source_root_fd = release_root_fd if target == "candidate" else backup_source_fd
                    source_parent, source_name = _open_parent_at(source_root_fd, relative)
                pinned.append((relative, release_entry, baseline_entry, destination_parent,
                               destination_name, source_parent, source_name))
            if target == "baseline":
                owner = self.state["destination_owner"]
                for (_relative, release_entry, baseline_entry, destination_parent,
                     destination_name, _source_parent, _source_name) in pinned:
                    if not baseline_entry["present"]:
                        _unlink_exact_candidate_at(
                            destination_parent, destination_name, release_entry, owner,
                            remove=False,
                        )
            mutation_started = True
            self._stop()
            owner = self.state["destination_owner"]
            for (relative, release_entry, baseline_entry, destination_parent, destination_name,
                 source_parent, source_name) in pinned:
                if target == "candidate":
                    data, _ = _read_at(source_parent, source_name)
                    _atomic_write_at(destination_parent, destination_name, data,
                                     int(release_entry["mode"], 8),
                                     uid=owner["uid"], gid=owner["gid"])
                elif baseline_entry["present"]:
                    data, _ = _read_at(source_parent, source_name)
                    _atomic_write_at(destination_parent, destination_name, data,
                                     int(baseline_entry["mode"], 8),
                                     uid=owner["uid"], gid=owner["gid"])
                else:
                    _unlink_exact_candidate_at(
                        destination_parent, destination_name, release_entry, owner,
                    )
                if after_boundary is not None:
                    after_boundary(f"{relative}:published")
            for relative, _release, _baseline, destination_parent, _name, source_parent, _source_name in pinned:
                destination_parent_path = self.destination.joinpath(*PurePosixPath(relative).parts[:-1])
                if not _same_namespace_inode(destination_parent, destination_parent_path):
                    raise ValueError("destination namespace changed")
                if source_parent is not None:
                    source_root = self.release_root if target == "candidate" else self.backup_root / "source"
                    source_parent_path = source_root.joinpath(*PurePosixPath(relative).parts[:-1])
                    if not _same_namespace_inode(source_parent, source_parent_path):
                        raise ValueError("source namespace changed")
            if self._phase(destination_root_fd) != target:
                raise RuntimeError("requested source phase did not verify")
            if not _same_namespace_inode(destination_root_fd, self.destination):
                raise ValueError("destination namespace changed")
            self.service.start_ready()
            if not self.service.is_active():
                raise RuntimeError("service is inactive after readiness")
            if not _same_namespace_inode(destination_root_fd, self.destination):
                raise ValueError("destination namespace changed")
            for relative, _release, _baseline, destination_parent, _name, _source_parent, _source_name in pinned:
                destination_parent_path = self.destination.joinpath(*PurePosixPath(relative).parts[:-1])
                if not _same_namespace_inode(destination_parent, destination_parent_path):
                    raise ValueError("destination namespace changed")
        except BaseException:
            if mutation_started:
                try:
                    self._stop()
                except BaseException:
                    pass
            raise
        finally:
            for item in pinned:
                os.close(item[3])
                if item[5] is not None:
                    os.close(item[5])
            os.close(destination_root_fd)
            os.close(release_root_fd)
            os.close(backup_source_fd)

    def recover(self, target: str):
        self.transition(target, operation="recover")

    def status(self):
        self._revalidate()
        phase = self._phase()
        baseline = self.state["service_baseline"]
        service = self.service.status(warning_since=baseline["warning_since"])
        required = {
            "service", "active", "substate", "http_status", "main_pid", "n_restarts",
            "control_group", "display_lock", "warning_count",
        }
        if type(service) is not dict:
            raise RuntimeError("status requires exact healthy service evidence")
        lock = service.get("display_lock")
        if (
            set(service) != required
            or service.get("service") != self.state["service"]
            or service.get("active") is not True
            or service.get("substate") != "running"
            or service.get("http_status") != 302
            or type(service.get("main_pid")) is not int
            or service["main_pid"] <= 0
            or type(service.get("n_restarts")) is not int
            or service["n_restarts"] != baseline["n_restarts"]
            or type(service.get("control_group")) is not str
            or not service["control_group"].endswith("/" + self.state["service"])
            or type(service.get("warning_count")) is not int
            or service["warning_count"] != 0
            or type(lock) is not dict
            or set(lock) != {"path", "dev", "ino", "uid", "gid", "mode", "locked"}
            or lock.get("path") != str(DISPLAY_LOCK)
            or lock.get("locked") is not True
            or any(type(lock.get(key)) is not int or lock[key] < 0 for key in ("dev", "ino", "uid", "gid"))
            or not re.fullmatch(r"[0-7]{4}", lock.get("mode", ""))
        ):
            raise RuntimeError("status requires exact healthy service evidence")
        service = dict(service)
        service["n_restarts_baseline"] = baseline["n_restarts"]
        return {"version": 1, "boundary": "b1", "source": phase,
                "source_sha": self.state["source_sha"] if phase == "candidate" else self.state["baseline_sha"],
                "service": service}

    def finalize(self):
        status = self.status()
        if status["source"] != "candidate":
            raise ValueError("finalize requires verified candidate source")
        report = {
            "version": 1, "boundary": "b1", "accepted_source_sha": self.state["source_sha"],
            "accepted_tree_sha": self.state["tree_sha"],
            "release_manifest_sha256": self.state["release_manifest"]["sha256"],
            "baseline_manifest_sha256": self.state["baseline_manifest"]["sha256"],
            "status": "accepted",
        }
        if set(report) != PUBLIC_FINAL_KEYS:
            raise RuntimeError("internal final report schema error")
        return report


def _baseline_manifest(approved: list[dict], baseline_sha: str, baseline_tree_sha: str):
    files = []
    for captured in approved:
        if captured["present"]:
            files.append({
                "path": captured["path"], "present": True,
                "sha256": captured["sha256"], "size": captured["size"],
                "mode": captured["mode"],
            })
        else:
            files.append({
                "path": captured["path"], "present": False,
                "sha256": None, "size": 0, "mode": None,
            })
    return {
        "version": 1, "boundary": "b1", "source_sha": baseline_sha,
        "tree_sha": baseline_tree_sha, "files": files,
    }


def _backup_metadata(metadata: os.stat_result, *, digest: str | None = None):
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
        metadata.st_mode, metadata.st_nlink, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_ctime_ns, digest,
    )


def _snapshot_backup_tree(root_fd: int):
    """Capture one complete tree only through an already admitted root descriptor."""
    records = {}

    def visit(directory_fd: int, prefix: PurePosixPath):
        for name in sorted(os.listdir(directory_fd)):
            relative = (prefix / name).as_posix()
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if _backup_metadata(opened) != _backup_metadata(before):
                        raise ValueError("published backup namespace changed")
                    records[relative] = ("directory", _backup_metadata(opened), None)
                    visit(child_fd, prefix / name)
                    if _backup_metadata(os.fstat(child_fd)) != _backup_metadata(opened):
                        raise ValueError("published backup changed while traversed")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode):
                fd = os.open(
                    name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                try:
                    data, opened = _read_descriptor(fd)
                finally:
                    os.close(fd)
                if _backup_metadata(opened) != _backup_metadata(before):
                    raise ValueError("published backup namespace changed")
                records[relative] = ("file", _backup_metadata(opened, digest=_sha(data)), data)
            else:
                raise ValueError("published backup contains an unsafe object")

    visit(root_fd, PurePosixPath())
    return records


def _revalidate_backup_namespace(backup: Path, root_fd: int, root_metadata: tuple,
                                 snapshot: dict):
    if not _same_namespace_inode(root_fd, backup):
        raise ValueError("backup namespace changed")
    if _backup_metadata(os.fstat(root_fd)) != root_metadata:
        raise ValueError("backup namespace changed")
    if _snapshot_backup_tree(root_fd) != snapshot:
        raise ValueError("backup namespace changed")


def _directory_authority_fd(path: Path, fd: int):
    metadata = os.fstat(fd)
    return {
        "path": str(path), "dev": metadata.st_dev, "ino": metadata.st_ino,
        "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _file_authority_snapshot(path: Path, record: tuple):
    kind, metadata, data = record
    if kind != "file":
        raise ValueError("backup manifest is not a regular file")
    dev, ino, uid, gid, mode, _nlink, _size, _mtime, _ctime, digest = metadata
    return {
        "path": str(path), "dev": dev, "ino": ino, "uid": uid, "gid": gid,
        "mode": f"{stat.S_IMODE(mode):04o}", "sha256": digest,
    }, data


def _validate_prepared_backup(backup: Path, approved: list[dict], baseline_manifest: dict,
                              *, root_fd: int | None = None):
    """Descriptor-pin and validate a published orphan through state publication."""
    if root_fd is None:
        root_fd = _open_directory(backup)
    root_info = os.fstat(root_fd)
    authority_owner = (os.geteuid(), os.getegid())
    if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
            or (root_info.st_uid, root_info.st_gid) != authority_owner
            or stat.S_IMODE(root_info.st_mode) != 0o700):
        os.close(root_fd)
        raise ValueError("published backup root is unsafe")
    try:
        root_metadata = _backup_metadata(root_info)
        snapshot = _snapshot_backup_tree(root_fd)
        expected_directories = {"source"}
        expected_files = {"baseline-source-manifest.json"}
        for captured in approved:
            if not captured["present"]:
                continue
            relative = PurePosixPath(captured["path"])
            expected_files.add((PurePosixPath("source") / relative).as_posix())
            parent = PurePosixPath("source")
            for part in relative.parts[:-1]:
                parent /= part
                expected_directories.add(parent.as_posix())
        actual_directories = {path for path, record in snapshot.items() if record[0] == "directory"}
        actual_files = {path for path, record in snapshot.items() if record[0] == "file"}
        if actual_directories != expected_directories or actual_files != expected_files:
            raise ValueError("published backup tree is incomplete or contains extras")
        for relative in expected_directories:
            metadata = snapshot[relative][1]
            if ((metadata[2], metadata[3]) != authority_owner
                    or stat.S_IMODE(metadata[4]) != 0o700):
                raise ValueError("published backup directory metadata mismatch")
        manifest_path = backup / "baseline-source-manifest.json"
        manifest_authority, raw = _file_authority_snapshot(
            manifest_path, snapshot["baseline-source-manifest.json"],
        )
        manifest_metadata = snapshot["baseline-source-manifest.json"][1]
        if ((manifest_metadata[2], manifest_metadata[3]) != authority_owner
                or stat.S_IMODE(manifest_metadata[4]) != 0o600
                or raw != canonical_json(baseline_manifest)):
            raise ValueError("published backup manifest mismatch")
        for captured in approved:
            if not captured["present"]:
                continue
            record = snapshot[(PurePosixPath("source") / captured["path"]).as_posix()]
            metadata, data = record[1], record[2]
            if ((metadata[2], metadata[3]) != authority_owner
                    or f"{stat.S_IMODE(metadata[4]):04o}" != captured["mode"]
                    or len(data) != captured["size"] or _sha(data) != captured["sha256"]):
                raise ValueError("published backup source mismatch")
        _revalidate_backup_namespace(backup, root_fd, root_metadata, snapshot)
        return {
            "fd": root_fd, "root_metadata": root_metadata, "snapshot": snapshot,
            "root_authority": _directory_authority_fd(backup, root_fd),
            "manifest_authority": manifest_authority,
        }
    except BaseException:
        os.close(root_fd)
        raise


def _remove_exact_backup_tree(root_fd: int, parent_fd: int, name: str, snapshot: dict):
    """Remove only the caller-owned tree still bound to its pinned descriptor."""
    root = os.fstat(root_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (root.st_dev, root.st_ino) != (current.st_dev, current.st_ino):
        return False
    if _snapshot_backup_tree(root_fd) != snapshot:
        return False
    files = [path for path, record in snapshot.items() if record[0] == "file"]
    directories = [path for path, record in snapshot.items() if record[0] == "directory"]
    for relative in sorted(files, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        directory_fd, leaf = _open_parent_at(root_fd, relative)
        try:
            current = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            if _backup_metadata(current, digest=snapshot[relative][1][-1]) != snapshot[relative][1]:
                return False
            os.unlink(leaf, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    for relative in sorted(directories, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        directory_fd, leaf = _open_parent_at(root_fd, relative)
        try:
            current = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (snapshot[relative][1][0], snapshot[relative][1][1]):
                return False
            os.rmdir(leaf, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (root.st_dev, root.st_ino) != (current.st_dev, current.st_ino):
        return False
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    return True


def _ensure_backup_parent_at(root_fd: int, relative: str):
    """Create/open one mode-0700 parent chain beneath a pinned private root."""
    parts = PurePosixPath(_safe_relative(relative)).parts[:-1]
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            metadata = os.fstat(child)
            if ((metadata.st_uid, metadata.st_gid) != (os.geteuid(), os.getegid())
                    or stat.S_IMODE(metadata.st_mode) != 0o700):
                os.close(child)
                raise ValueError("temporary backup directory metadata mismatch")
            os.fsync(current)
            os.close(current)
            current = child
        return current, PurePosixPath(relative).name
    except BaseException:
        os.close(current)
        raise


def _fsync_backup_tree_at(root_fd: int):
    """Durably flush every admitted directory from leaves to the pinned root."""
    for name in sorted(os.listdir(root_fd)):
        metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                _fsync_backup_tree_at(child)
            finally:
                os.close(child)
        elif not stat.S_ISREG(metadata.st_mode):
            raise ValueError("temporary backup contains an unsafe object")
    os.fsync(root_fd)


def _build_and_publish_backup(backup: Path, approved: list[dict], baseline_manifest: dict):
    parent = backup.parent
    parent_fd = _open_directory(parent)
    temporary_name = _fresh_sibling_name(backup)
    temporary_fd = -1
    published = False
    authority = None
    try:
        if not _same_namespace_inode(parent_fd, parent):
            raise ValueError("backup parent namespace changed")
        os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        temporary_fd = os.open(
            temporary_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        root = os.fstat(temporary_fd)
        if ((root.st_uid, root.st_gid) != (os.geteuid(), os.getegid())
                or stat.S_IMODE(root.st_mode) != 0o700):
            raise ValueError("temporary backup root metadata mismatch")
        os.mkdir("source", mode=0o700, dir_fd=temporary_fd)
        os.fsync(temporary_fd)
        for captured in approved:
            if not captured["present"]:
                continue
            relative = (PurePosixPath("source") / captured["path"]).as_posix()
            destination_parent, destination_name = _ensure_backup_parent_at(
                temporary_fd, relative,
            )
            try:
                _atomic_write_at(
                    destination_parent, destination_name, captured["data"],
                    int(captured["mode"], 8), uid=os.geteuid(), gid=os.getegid(),
                )
            finally:
                os.close(destination_parent)
        _atomic_write_at(
            temporary_fd, "baseline-source-manifest.json",
            canonical_json(baseline_manifest), 0o600,
            uid=os.geteuid(), gid=os.getegid(),
        )
        _fsync_backup_tree_at(temporary_fd)
        temporary_snapshot = _snapshot_backup_tree(temporary_fd)
        if not _same_namespace_inode(parent_fd, parent):
            raise ValueError("backup parent namespace changed")
        try:
            _rename_noreplace(temporary_name, backup.name, parent_fd=parent_fd)
        except FileExistsError:
            _remove_exact_backup_tree(temporary_fd, parent_fd, temporary_name, temporary_snapshot)
        else:
            published = True
            os.fsync(parent_fd)
        if not _same_namespace_inode(parent_fd, parent):
            raise ValueError("backup parent namespace changed")
        if published:
            admitted_fd = temporary_fd
            temporary_fd = -1
            authority = _validate_prepared_backup(
                backup, approved, baseline_manifest, root_fd=admitted_fd,
            )
    except BaseException:
        if temporary_fd >= 0 and not published:
            try:
                snapshot = _snapshot_backup_tree(temporary_fd)
                _remove_exact_backup_tree(temporary_fd, parent_fd, temporary_name, snapshot)
            except (FileNotFoundError, OSError, ValueError):
                pass
        raise
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        os.close(parent_fd)
    if authority is not None:
        return authority
    return _validate_prepared_backup(backup, approved, baseline_manifest)


def prepare_b1(arguments):
    release_root = _absolute(arguments.release_root, "release root", must_exist=True)
    release_manifest_path = _absolute(arguments.release_manifest, "release manifest", must_exist=True)
    fixture_manifest_path = _absolute(arguments.fixture_manifest, "fixture manifest", must_exist=True)
    destination = _absolute(arguments.destination, "destination", must_exist=True)
    backup = _absolute(arguments.backup_root, "backup root")
    state_path = _absolute(arguments.state, "state")
    interpreter = _absolute(arguments.interpreter, "interpreter", must_exist=True)
    if not release_root.is_dir() or release_root.is_symlink() or not destination.is_dir() or destination.is_symlink():
        raise ValueError("release and destination must be real directories")
    if any(first == second for index, first in enumerate((release_root, destination, backup, state_path.parent))
           for second in (release_root, destination, backup, state_path.parent)[index + 1:]):
        raise ValueError("release, destination, backup, and authority roots must be distinct")
    if SERVICE.fullmatch(arguments.service or "") is None:
        raise ValueError("service name is invalid")
    interpreter_target = interpreter.resolve(strict=True)
    interpreter_info = interpreter_target.lstat()
    if not stat.S_ISREG(interpreter_info.st_mode) or not os.access(interpreter, os.X_OK):
        raise ValueError("interpreter is unsafe")
    release_raw, _ = _read_regular(release_manifest_path)
    fixture_raw, _ = _read_regular(fixture_manifest_path)
    release = _validate_release_manifest(release_raw, release_root)
    fixture = _validate_fixture_manifest(fixture_raw, fixture_manifest_path.parent)
    expected = {
        "baseline_sha": arguments.expected_baseline_source_sha,
        "source_sha": arguments.expected_source_sha,
        "tree_sha": arguments.expected_tree_sha,
    }
    for value in (*expected.values(), arguments.expected_baseline_tree_sha):
        if SHA1.fullmatch(value or "") is None:
            raise ValueError("expected Git identity is invalid")
    if any(release[key] != value for key, value in expected.items()):
        raise ValueError("release manifest expected identity mismatch")
    if (fixture["source_sha"], fixture["tree_sha"], _sha(fixture_raw)) != (
            arguments.expected_source_sha, arguments.expected_tree_sha, release["fixture_manifest_sha256"]):
        raise ValueError("fixture manifest expected identity mismatch")
    destination_info = destination.lstat()
    destination_owner = {"uid": destination_info.st_uid, "gid": destination_info.st_gid}
    approved = _approved_baseline_snapshot(
        destination, release, arguments.expected_baseline_source_sha,
        arguments.expected_baseline_tree_sha, destination_owner,
    )
    if state_path.exists() or state_path.is_symlink():
        raise ValueError("state authority must be fresh")
    if not state_path.parent.exists():
        parent_parent = state_path.parent.parent
        parent_info = parent_parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise ValueError("state ancestry is unsafe")
        try:
            state_path.parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            _fsync_dir(state_path.parent.parent)
    if state_path.parent.is_symlink() or not state_path.parent.is_dir():
        raise ValueError("state parent is unsafe")
    if state_path.parent.resolve(strict=True) != state_path.parent:
        raise ValueError("state ancestry is unsafe")

    baseline_manifest = _baseline_manifest(
        approved, arguments.expected_baseline_source_sha,
        arguments.expected_baseline_tree_sha,
    )
    service = SystemdService(arguments.service)
    if not service.is_active():
        raise ValueError("prepare-b1 requires the baseline service active")
    initial_service = service.status()
    if (type(initial_service) is not dict or initial_service.get("service") != arguments.service
            or type(initial_service.get("n_restarts")) is not int
            or initial_service["n_restarts"] < 0):
        raise ValueError("baseline service evidence is invalid")
    service_baseline = {
        "n_restarts": initial_service["n_restarts"], "warning_since": int(time.time()),
    }

    if backup.exists() or backup.is_symlink():
        backup_authority = _validate_prepared_backup(backup, approved, baseline_manifest)
    else:
        backup_authority = _build_and_publish_backup(backup, approved, baseline_manifest)

    try:
        parent_fd = _open_directory(state_path.parent)
    except BaseException:
        os.close(backup_authority["fd"])
        raise
    state_fd = -1
    temporary_name = _fresh_sibling_name(state_path)
    temporary_identity = None
    state_published = False
    completed = False
    try:
        state_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600, dir_fd=parent_fd,
        )
        os.fchmod(state_fd, 0o600)
        os.fchown(state_fd, os.geteuid(), os.getegid())
        temporary_metadata = os.fstat(state_fd)
        temporary_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)
        fcntl.flock(state_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {
            "version": 1, "boundary": "b1", "baseline_sha": arguments.expected_baseline_source_sha,
            "baseline_tree_sha": arguments.expected_baseline_tree_sha,
            "source_sha": arguments.expected_source_sha, "tree_sha": arguments.expected_tree_sha,
            "release_root": _directory_authority(release_root), "release_manifest": _manifest_file(release_manifest_path),
            "fixture_manifest": _manifest_file(fixture_manifest_path),
            "baseline_manifest": backup_authority["manifest_authority"],
            "destination": _directory_authority(destination),
            "destination_owner": destination_owner,
            "backup_root": backup_authority["root_authority"], "service": arguments.service,
            "service_baseline": service_baseline, "interpreter": str(interpreter),
            "state_path": str(state_path), "self": None,
        }
        _populate_state_fd(state_path, state, state_fd)
        _revalidate_backup_namespace(
            backup, backup_authority["fd"], backup_authority["root_metadata"],
            backup_authority["snapshot"],
        )
        _rename_noreplace(temporary_name, state_path.name, parent_fd=parent_fd)
        state_published = True
        _revalidate_backup_namespace(
            backup, backup_authority["fd"], backup_authority["root_metadata"],
            backup_authority["snapshot"],
        )
        if not _same_namespace_inode(state_fd, state_path):
            raise ValueError("state identity changed after publication")
        os.fsync(parent_fd)
        tx = Priority15Transaction(state_path, state, service=service, state_fd=state_fd)
        state_fd = -1
        try:
            tx._revalidate()
        finally:
            tx.close()
        completed = True
        return state
    finally:
        if state_published and not completed:
            try:
                current = os.stat(state_path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == temporary_identity:
                    os.unlink(state_path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
        if state_fd >= 0:
            os.close(state_fd)
        if temporary_identity is not None:
            try:
                current = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == temporary_identity:
                    os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)
        os.close(backup_authority["fd"])


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-b1")
    for option in ("--repository", "--baseline-commit", "--source-commit",
                   "--expected-reviewed-diff-sha256", "--stage-root", "--python"):
        add_store_once_argument(build, option, required=True)
    prepare = commands.add_parser("prepare-b1")
    for option in ("--release-root", "--release-manifest", "--fixture-manifest",
                   "--expected-baseline-source-sha", "--expected-baseline-tree-sha",
                   "--expected-source-sha", "--expected-tree-sha", "--destination",
                   "--backup-root", "--service", "--interpreter", "--state"):
        add_store_once_argument(prepare, option, required=True)
    for command in ("deploy", "status", "rollback", "redeploy", "finalize"):
        sub = commands.add_parser(command)
        add_store_once_argument(sub, "--state", required=True)
    recover = commands.add_parser("recover")
    add_store_once_argument(recover, "--state", required=True)
    add_store_once_argument(recover, "--target", choices=("baseline", "candidate"), required=True)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    if arguments.command == "build-b1":
        print(canonical_json(build_b1(arguments)).decode(), end="")
        return 0
    if arguments.command == "prepare-b1":
        prepare_b1(arguments)
        return 0
    tx = Priority15Transaction.load(Path(arguments.state))
    if arguments.command == "deploy":
        tx.transition("candidate", operation="deploy")
    elif arguments.command == "rollback":
        tx.transition("baseline", operation="rollback")
    elif arguments.command == "redeploy":
        tx.transition("candidate", operation="redeploy")
    elif arguments.command == "recover":
        tx.recover(arguments.target)
    elif arguments.command == "status":
        print(canonical_json(tx.status()).decode(), end="")
    else:
        print(canonical_json(tx.finalize()).decode(), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"priority-1.5 transaction failed: {error}", file=sys.stderr)
        raise SystemExit(2)
