"""
On-disk store of national radar3d volumes, one uncompressed `.npy` per
product and cycle under `RADAR3D_DATA_DIR/<product>/<YYYYMMDDTHHMMZ>.npy`.
Files are memory-mapped when served, so a viewport crop is a slice the OS
page cache serves. Postgres (`radar3d_frames`) only keeps the index.
"""
import datetime
import json
import os
from pathlib import Path

import numpy as np


TS_FORMAT = '%Y%m%dT%H%MZ'


class FrameMissing(LookupError):
    pass


class FrameStore:

    def __init__(self, root):
        self.root = Path(root)

    def path(self, product, ts):
        return self.root / product / f'{ts:{TS_FORMAT}}.npy'

    def write(self, product, ts, array, dtype=np.uint8):
        path = self.path(product, ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'wb') as f:
            np.save(f, np.ascontiguousarray(array, dtype=dtype))
        os.replace(tmp, path)
        return path

    def json_path(self, product, ts):
        return self.root / product / f'{ts:{TS_FORMAT}}.json'

    def write_json(self, product, ts, data):
        path = self.json_path(product, ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(data, separators=(',', ':')))
        os.replace(tmp, path)
        return path

    def read_json(self, product, ts):
        path = self.json_path(product, ts)
        if not path.is_file():
            raise FrameMissing(f'No {product} data for {ts:%Y-%m-%dT%H:%MZ}')
        return json.loads(path.read_text())

    def open(self, product, ts):
        path = self.path(product, ts)
        if not path.is_file():
            raise FrameMissing(f'No {product} frame for {ts:%Y-%m-%dT%H:%MZ}')
        return np.load(path, mmap_mode='r')

    def crop(self, product, ts, crop, scale=1):
        vol = self.open(product, ts)
        sub = np.asarray(vol[:, crop.row0:crop.row1, crop.col0:crop.col1])
        if scale > 1:
            levels, height, width = sub.shape
            sub = sub.reshape(
                levels, height // scale, scale, width // scale, scale,
            ).max(axis=(2, 4))
        return sub

    def timestamps(self, product):
        out = set()
        for path in (self.root / product).glob('*.*'):
            if path.suffix not in ('.npy', '.json'):
                continue
            try:
                ts = datetime.datetime.strptime(path.stem, TS_FORMAT)
            except ValueError:
                continue
            out.add(ts.replace(tzinfo=datetime.UTC))
        return sorted(out)

    def delete_before(self, product, cutoff):
        deleted = []
        for ts in self.timestamps(product):
            if ts < cutoff:
                self.path(product, ts).unlink(missing_ok=True)
                self.json_path(product, ts).unlink(missing_ok=True)
                deleted.append(ts)
        return deleted


def index_frame(conn, product, ts, path, sites):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO radar3d_frames (product, timestamp, path, sites)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT radar3d_frames_key DO UPDATE SET
              path = EXCLUDED.path,
              sites = EXCLUDED.sites,
              created_at = current_timestamp
            """,
            (product, ts, str(path), sites),
        )
    conn.commit()


def indexed_timestamps(conn, product, since):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timestamp FROM radar3d_frames
            WHERE product = %s AND timestamp >= %s
            """,
            (product, since),
        )
        return {row[0] for row in cur.fetchall()}


def delete_index_before(conn, product, cutoff):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM radar3d_frames WHERE product = %s AND timestamp < %s",
            (product, cutoff),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted


def indexed_products(conn, prefix):
    """Distinct product names in the index starting with `prefix`."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT product FROM radar3d_frames "
            "WHERE product LIKE %s",
            (prefix + '%',),
        )
        return {row[0] for row in cur.fetchall()}
