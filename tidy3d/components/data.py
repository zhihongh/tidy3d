import pydantic
from abc import ABC, abstractmethod
import xarray as xr
import numpy as np
import holoviews as hv
import os
import json
import h5py

from .simulation import Simulation
from .monitor import Monitor, FluxMonitor, FieldMonitor, ModeMonitor, FreqSampler
from .types import Dict


class Tidy3dData(pydantic.BaseModel):
    """base class for data associated with a specific task."""

    class Config:
        """sets config for all Tidy3dBaseModel objects"""

        validate_all = True  # validate default values too
        extra = "forbid"  # forbid extra kwargs not specified in model
        validate_assignment = True  # validate when attributes are set after initialization
        arbitrary_types_allowed = True  # allow us to specify a type for an arg that is an arbitrary class (np.ndarray)
        allow_mutation = False  # dont allow one to change the data


class MonitorData(xr.DataArray, ABC):
    __slots__ = ()  # need this for xarray subclassing

    def __eq__(self, other):
        assert isinstance(other, MonitorData), "can only check eqality on two monitor data objects"
        return self.equals(other)

    def sampler_label(self):
        return "freqs" if "freqs" in self.coords else "times"

    @abstractmethod
    def visualize(self, simulation):
        """make interactive plot"""
        pass

    def export_as_file(self, path: str) -> None:
        """Export MonitorData to hdf5 file (named this to avoid namespace conflicts with xarray)"""
        self.to_netcdf(path=path, engine="h5netcdf", invalid_netcdf=True)

    @classmethod
    def load_from_file(cls, path: str):
        """Load MonitorData from hdf5 file (named this to avoid namespace conflicts with xarray)"""
        data_array = xr.open_dataarray(path, engine="h5netcdf")
        return cls(data_array)


class FieldData(MonitorData):
    __slots__ = ()  # need this for xarray subclassing

    def visualize(self):
        """make interactive plot"""
        hv_ds = hv.Dataset(self.copy())
        image = hv_ds.to(hv.Image, kdims=["xs", "ys"], dynamic=True)
        return image.options(cmap="RdBu", colorbar=True, aspect="equal")


class FluxData(MonitorData):
    __slots__ = ()  # need this for xarray subclassing

    def visualize(self):
        """make interactive plot"""
        hv.extension("bokeh")
        hv_ds = hv.Dataset(self.copy())
        image = hv_ds.to(hv.Curve, self.sampler_label())
        return image


class ModeData(MonitorData):
    __slots__ = ()  # need this for xarray subclassing

    def visualize(self):
        """make interactive plot"""
        hv_ds = hv.Dataset(self.copy())
        image = hv_ds.to(hv.Curve, self.sampler_label(), dynamic=True)
        return image


# maps monitor type to corresponding data type
monitor_data_map = {FieldMonitor: FieldData, FluxMonitor: FluxData, ModeMonitor: ModeData}

data_dim_map = {
    FieldData: ["field", "component", "xs", "ys", "zs", "sampler_value(replace)"],
    FluxData: ["sampler_value(replace)"],
    ModeData: ["direction", "mode_index", "sampler_value(replace)"],
}


class SimulationData(Tidy3dData):

    simulation: Simulation
    monitor_data: Dict[str, MonitorData]

    def export(self, path: str) -> None:
        """Export all data to a file"""

        # write to the file at path
        with h5py.File(path, "a") as f:

            # save json string as an attribute
            json_string = self.simulation.json()
            f.attrs["json_string"] = json_string

            # make a group for monitor_data
            mon_data_grp = f.create_group("monitor_data")
            for mon_name, mon_data in self.monitor_data.items():

                # for each monitor, make new group with the same name
                mon_grp = mon_data_grp.create_group(mon_name)

                # add the data value to the moniitor
                mon_grp.create_dataset("data", data=mon_data.data)

                # for each of the coordinates
                for coord_name in mon_data.coords:

                    # get the data and convert it to the correct type if it contains strings
                    coord_val = mon_data[coord_name].data
                    if isinstance(coord_val[0], np.str_):
                        dt = h5py.special_dtype(vlen=str)
                        coord_val = np.array(coord_val, dtype=dt)

                    # add the data to the group
                    mon_grp.create_dataset(coord_name, data=coord_val)

    @classmethod
    def load(cls, path: str):
        """Load SimulationData from files"""

        # read from file at path
        with h5py.File(path, "r") as f:

            # construct the original simulation from the json string
            json_string = f.attrs["json_string"]
            sim = Simulation.parse_raw(json_string)

            # loop through monitor dataset and create all MonitorData instances
            monitor_data_dict = {}
            monitor_data = f["monitor_data"]
            for mon_name, mon_data in monitor_data.items():

                # get info about the original monitor
                monitor = sim.monitors.get(mon_name)
                assert monitor is not None, "monitor not found in original simulation"
                mon_type = type(monitor)
                data_type = monitor_data_map[mon_type]

                # get the dimensions for this data type, replace sampler data with correct value
                dims = data_dim_map[data_type]
                sampler_dim = "freqs" if isinstance(monitor.sampler, FreqSampler) else "times"
                dims[-1] = sampler_dim

                # load data from dataset, separate data and coordinates
                coords = {}
                for data_name, data_value in mon_data.items():

                    # convert bytes to string if neceessary and add to dict
                    if isinstance(data_value[0], bytes):
                        data_value = np.array([v.decode("UTF-8") for v in data_value])
                    coords[data_name] = data_value

                data_value = coords.pop("data")

                # load into an xarray.DataArray and make a monitor data to append to dictionary
                darray = xr.DataArray(data_value, coords, dims=dims, name=mon_name)
                monitor_data_instance = data_type(darray)
                monitor_data_dict[mon_name] = monitor_data_instance

        # return a SimulationData object containing all of the information
        return cls(simulation=sim, monitor_data=monitor_data_dict)