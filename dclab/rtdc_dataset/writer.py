import copy
import pathlib

import h5py
import numpy as np

from .. import definitions as dfn
from .._version import version

#: Chunk size for storing HDF5 data
CHUNK_SIZE = 100


class RTDCWriter:
    def __init__(self, path_or_h5file, mode="append", compression="gzip"):
        """RT-DC data writer classe

        Parameters
        ----------
        path_or_h5file: str or pathlib.Path or h5py.Group
            Path to an HDF5 file or an HDF5 file opened in write mode
        mode: str
            Defines how the data are stored:
            - "append": append new feature data to existing h5py Datasets
            - "replace": replace existing h5py Datasets with new features
                         (used for ancillary feature storage)
            - "reset": do not keep any previous data
        compression: str
            Compression method used for data storage;
            one of [None, "lzf", "gzip", "szip"].
        """
        assert mode in ["append", "replace", "reset"]
        self.mode = mode
        self.compression = compression
        if isinstance(path_or_h5file, h5py.Group):
            self.path = pathlib.Path(path_or_h5file.file.filename)
            self.h5file = path_or_h5file
        else:
            self.path = pathlib.Path(path_or_h5file)
            self.h5file = h5py.File(path_or_h5file,
                                    mode=("w" if mode == "reset" else "a"))

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        # close the HDF5 file
        self.h5file.close()

    def store_feature(self, feat, data):
        """Write feature data"""
        if not dfn.feature_exists(feat):
            raise ValueError(f"Undefined feature '{feat}'!")

        events = self.h5file.require_group("events")

        # replace data?
        if feat in events and self.mode == "replace":
            if feat == "trace":
                for tr_name in data.keys():
                    if tr_name in events[feat]:
                        del events[feat][tr_name]
            else:
                del events[feat]

        if dfn.scalar_feature_exists(feat):
            self.write_ndarray(group=events,
                               name=feat,
                               data=np.atleast_1d(data))
        elif feat == "contour":
            self.write_ragged(group=events, name=feat, data=data)
        elif feat in ["image", "image_bg", "mask"]:
            self.write_image_grayscale(group=events,
                                       name=feat,
                                       data=data)
        elif feat == "trace":
            for tr_name in data.keys():
                # verify trace names
                if tr_name not in dfn.FLUOR_TRACES:
                    raise ValueError(f"Unknown trace key: '{tr_name}'!")
                # write trace
                self.write_ndarray(group=events.require_group("trace"),
                                   name=tr_name,
                                   data=np.atleast_2d(data[tr_name])
                                   )
        else:
            raise NotImplementedError(f"No rule to store feature {feat}!")

    def store_log(self, name, lines):
        """Write log data

        Parameters
        ----------
        name: str
            name of the log entry
        lines: list of str
            the text lines of the log
        """
        log_group = self.h5file.require_group("logs")
        self.write_text(group=log_group, name=name, lines=lines)

    def store_metadata(self, meta, version_brand=True):
        """Store RT-DC meradata

        Parameters
        ----------
        meta: dict-like
            The meta data to store. Each key depicts a meta data section
            name whose data is given as a dictionary, e.g.::

                meta = {"imaging": {"exposure time": 20,
                                    "flash duration": 2,
                                    ...
                                    },
                        "setup": {"channel width": 20,
                                  "chip region": "channel",
                                  ...
                                  },
                        ...
                        }

            Only section key names and key values therein registered
            in dclab are allowed and are converted to the pre-defined
            dtype. If you have custom metadata, you can use the "user"
            section.
        version_brand: bool
            Whether or not to append a "| dclab X.Y.Z" to the software
            version string.
        """
        meta = copy.deepcopy(meta)
        # Check meta data
        for sec in meta:
            if sec == "user":
                # user-defined metadata are always written.
                # Any errors (incompatibilities with HDF5 attributes)
                # are the user's responsibility
                continue
            elif sec not in dfn.CFG_METADATA:
                # only allow writing of meta data that are not editable
                # by the user (not dclab.dfn.CFG_ANALYSIS)
                raise ValueError(
                    f"Meta data section not defined in dclab: {sec}")
            for ck in meta[sec]:
                if not dfn.config_key_exists(sec, ck):
                    raise ValueError(
                        f"Meta key not defined in dclab: {sec}:{ck}")

        if version_brand:
            # update version
            # - if it is not already in the hdf5 file (prevent override)
            # - if it is explicitly given in meta (append to old version
            #   string)
            if ("setup:software version" not in self.h5file.attrs
                    or ("setup" in meta and "software version" in meta[
                        "setup"])):
                thisver = "dclab {}".format(version)
                if "setup" in meta and "software version" in meta["setup"]:
                    oldver = meta["setup"]["software version"]
                    thisver = f"{oldver} | {thisver}"
                if "setup" not in meta:
                    meta["setup"] = {}
                meta["setup"]["software version"] = thisver

        # Write metadata
        for sec in meta:
            for ck in meta[sec]:
                idk = f"{sec}:{ck}"
                value = meta[sec][ck]
                if isinstance(value, bytes):
                    # We never store byte attribute values.
                    # In this case, `conffunc` should be `str` or `lcstr` or
                    # somesuch. But we don't test that, because no other
                    # datatype competes with str for bytes.
                    value = value.decode("utf-8")
                if sec == "user":
                    # store user-defined metadata as-is
                    self.h5file.attrs[idk] = value
                else:
                    # pipe the metadata through the hard-coded converter
                    # functions
                    convfunc = dfn.get_config_value_func(sec, ck)
                    self.h5file.attrs[idk] = convfunc(value)

    def write_image_grayscale(self, group, name, data):
        """Write grayscale image data to and HDF5 dataset
        """
        data = np.atleast_2d(data)
        if len(data.shape) == 2:
            # put single event in 3D array
            data = data.reshape(1, data.shape[0], data.shape[1])

        # convert binary data (mask) to uint8
        if data.dtype == np.bool:
            data = np.asarray(data, dtype=np.uint8) * 255

        dset = self.write_ndarray(group=group, name=name, data=data)

        # Create and Set image attributes:
        # HDFView recognizes this as a series of images.
        # Use np.string_ as per
        # http://docs.h5py.org/en/stable/strings.html#compatibility
        dset.attrs.create('CLASS', np.string_('IMAGE'))
        dset.attrs.create('IMAGE_VERSION', np.string_('1.2'))
        dset.attrs.create('IMAGE_SUBCLASS', np.string_('IMAGE_GRAYSCALE'))

    def write_ndarray(self, group, name, data, dtype=None):
        """Write n-dimensional array data to an HDF5 dataset

        It is assumed that the shape of the array data is correct,
        i.e. that the shape of `data` is
        (number_events, feat_shape_1, ..., feat_shape_n).
        """
        if name not in group:
            maxshape = tuple([None] + list(data.shape)[1:])
            chunks = tuple([CHUNK_SIZE] + list(data.shape)[1:])
            dset = group.create_dataset(
                name,
                shape=data.shape,
                dtype=dtype or data.dtype,
                maxshape=maxshape,
                chunks=chunks,
                fletcher32=True,
                compression=self.compression)
            offset = 0
        else:
            dset = group[name]
            offset = dset.shape[0]
            dset.resize(offset + data.shape[0], axis=0)
        if len(data.shape) == 1:
            # store scalar data in one go
            dset[offset:] = data
        else:
            # populate higher-dimensional data in chunks
            # (reduces file size, memory usage, and saves time)
            num_chunks = len(data) // CHUNK_SIZE
            for ii in range(num_chunks):
                start = ii * CHUNK_SIZE
                stop = start + CHUNK_SIZE
                dset[offset+start:offset+stop] = data[start:stop]
            # write remainder (if applicable)
            num_remain = len(data) % CHUNK_SIZE
            if num_remain:
                start_e = num_chunks*CHUNK_SIZE
                stop_e = start_e + num_remain
                dset[offset+start_e:offset+stop_e] = data[start_e:stop_e]
        return dset

    def write_ragged(self, group, name, data):
        """Write ragged data (i.e. list of arrays of different lenghts)

        Ragged array data (e.g. contour data) are stored in
        a separate group and each entry becomes an HDF5 dataset.
        """
        if not isinstance(data, (list, tuple)):
            # place single event in list
            data = [data]
        grp = group.require_group(name)
        curid = len(grp.keys())
        for ii, cc in enumerate(data):
            grp.create_dataset("{}".format(curid + ii),
                               data=cc,
                               fletcher32=True,
                               chunks=cc.shape,
                               compression=self.compression)

    def write_text(self, group, name, lines):
        """Write text to an HDF5 dataset

        Text data are written as as fixed-length string dataset.

        Parameters
        ----------
        group: h5py.Group
            parent group
        name: str
            name of the dataset containing the text
        lines: list of str
            the text, line by line
        """
        # replace text?
        if name in group and self.mode == "replace":
            del group[name]

        # handle strings
        if isinstance(lines, (str, bytes)):
            lines = [lines]

        lnum = len(lines)
        # Determine the maximum line length and use fixed-length strings,
        # because compression and fletcher32 filters won't work with
        # variable length strings.
        # https://github.com/h5py/h5py/issues/1948
        # 100 is the recommended maximum and the default, because if
        # `mode` is e.g. "append", then this line may not be the longest.
        max_length = 100
        lines_as_bytes = []
        for line in lines:
            # convert lines to bytes
            if not isinstance(line, bytes):
                lbytes = line.encode("UTF-8")
            else:
                lbytes = line
            max_length = max(max_length, len(lbytes))
            lines_as_bytes.append(lbytes)

        if name not in group:
            # Create the dataset
            txt_dset = group.create_dataset(
                name,
                (lnum,),
                dtype=f"S{max_length}",
                maxshape=(None,),
                chunks=True,
                fletcher32=True,
                compression=self.compression)
            line_offset = 0
        else:
            # TODO: test whether fixed length is long enough!
            # Resize the dataset
            txt_dset = group[name]
            line_offset = txt_dset.shape[0]
            txt_dset.resize(line_offset + lnum, axis=0)

        # Write the text data line-by-line
        for ii, lbytes in enumerate(lines_as_bytes):
            txt_dset[line_offset + ii] = lbytes
