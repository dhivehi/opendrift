# This file is part of OpenDrift.
#
# OpenDrift is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 2
#
# OpenDrift is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OpenDrift.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright 2015, Knut-Frode Dagestad, MET Norway

import sys
import os
import glob
import types
import traceback
import logging
from datetime import datetime, timedelta
from collections import OrderedDict
from abc import ABCMeta, abstractmethod, abstractproperty

import numpy as np
import configobj, validate
try:
    from mpl_toolkits.basemap import Basemap
    import matplotlib.pyplot as plt
    from matplotlib import animation
    from matplotlib.patches import Polygon
    have_nx = True
    try:
        import matplotlib.nxutils as nx
    except:
        have_nx = False
        from matplotlib.path import Path
except:
    logging.info('Basemap is not available, can not make plots')

from opendrift.readers.basereader import pyproj, BaseReader, vector_pairs_xy
from opendrift.models.physics_methods import PhysicsMethods


class ModelSettings(object):
    # Empty class to store model specific information,
    # to avoid namespace conflicts with OpenDriftSimulation class
    pass


class OpenDriftSimulation(PhysicsMethods):
    """Generic trajectory model class, to be extended (subclassed).

    This as an Abstract Base Class, meaning that only subclasses can
    be initiated and used.
    Any specific subclass ('model') must contain its own (or shared)
    specific type of particles (ElementType), whose properties are
    updated at each time_step using method update() on basis of model
    physics/chemistry/biology and 'required_variables' (environment)
    which are provided by one or more Reader objects.

    Attributes:
        ElementType: the type (class) of particles to be used by this model
        elements: object of the class ElementType, storing the specific
            particle properties (ndarrays and scalars) of all active particles
            as named attributes. Elements are added by seeding-functions
            (presently only one implemented: seed_elements).
        elements_deactivated: ElementType object containing particles which
            have been deactivated (and removed from 'elements')
        elements_scheduled: ElementType object containing particles which
            have been scheduled, but not yet activated
        required_variables: list of strings of CF standard_names which is
            needed by this model (update function) to update properties of
            particles ('elements') at each time_step. This core class has
            no required_elements, this is implemented by subclasses/modules.
        environment: recarray storing environment variables (wind, waves,
            current etc) as named attributes. Attribute names follow
            standard_name from CF-convention, allowing any OpenDriftSimulation
            module/subclass using environment data from any readers which
            can provide the requested variables. Used in method 'update'
            to update properties of elements every time_step.
        proj4: string defining the common spatial reference system (SRS) onto
            which data from all readers are interpolated
        proj: Proj object initialised from proj4 string; used for
            coordinate tranformations
        time_step: timedelta object, time interval at which element properties
            are updated (including advection).
        time_step_output: timedelta object, time interval at which element
            properties are stored in memory and eventually written to file
        readers: Dictionary where values are Reader objects, and names are
            unique reference keywords used to access a given reader (typically
            filename or URL)
        priority_list: OrderedDict where names are variable names,
            and values are lists of names (kewywords) of the reader, in the
            order of priority (user defined) of which the readers shall be
            called to retrieve environmental data.
    """

    __metaclass__ = ABCMeta

    status_categories = ['active']  # Particles are active by default

    # Default plotting colors of trajectory endpoints
    status_colors_default = {'initial': 'green',
                             'active': 'blue',
                             'missing_data': 'gray'}

    model = ModelSettings  # To store model specific information

    configspec_basemodel = '''
            [general]
                basemap_resolution = option('f', 'h', 'i', 'c', default='h')
                coastline_action = option('none', 'stranding', 'previous', default='stranding')
            [drift]
                scheme = option('euler', 'runge-kutta', default='euler')
                wind_drift_factor = float(min=0, max=1, default=0.02)
                stokes_drift = boolean(default=True)
                current_uncertainty = float(min=0, max=5, default=.1)
                wind_uncertainty = float(min=0, max=5, default=1)
                relative_wind = boolean(default=False)'''

    max_speed = 1  # Assumed max average speed of any element
    required_profiles = None  # Optional possibility to get vertical profiles
    required_profiles_z_range = None  # [min_depth, max_depth]

    def __init__(self, proj4=None, seed=0, iomodule='netcdf',
                 loglevel=logging.DEBUG):
        """Initialise OpenDriftSimulation

        Args:
            proj4: proj4 string defining spatial reference system.
                If not specified, SRS is taken from the first added reader.
            seed: integer or None. A given integer will yield identical
                random numbers drawn each simulation. Random numbers are
                e.g. used to distribute particles spatially when seeding,
                and may be used by modules (subclasses) for e.g. diffusion.
                Specifying a fixed value (default: 0) is useful for sensitivity
                tests. With seed = None, different random numbers will be drawn
                for subsequent runs, even with identical configuration/input.
            iomodule: name of module used to export data
                default: netcdf, see folder 'io' for more alternatives.
                'iomodule' is module/filename without preceeding 'io_'
            loglevel: set to 0 (default) to retrieve all debug information.
                Provide a higher value (e.g. 20) to receive less output.
                Use the string 'custom' to configure logging from outside.
        """

        # Set default configuration
        self._add_configstring(self.configspec_basemodel)

        # Dict to store readers
        self.readers = {}  # Dictionary, key=name, value=reader object
        self.priority_list = OrderedDict()
        self.use_block = True  # Set to False if interpolation left to reader

        if not hasattr(self, 'fallback_values'):
            self.fallback_values = {}

        # Make copies of dictionaries so that they are private to each instance
        self.status_categories = ['active']  # Particles are active by default
        self.fallback_values = self.fallback_values.copy()
        self.status_colors_default = self.status_colors_default.copy()

        if hasattr(self, 'status_colors'):
            # Append model specific colors to (and override) default colors
            self.status_colors_default.update(self.status_colors)
            self.status_colors = self.status_colors_default
        else:
            self.status_colors = self.status_colors_default

        # Set projection, if given
        self.set_projection(proj4)

        # Using a fixed seed will generate the same random numbers
        # each run, useful for sensitivity tests
        # Use seed = None to get different random numbers each time
        np.random.seed(seed)

        self.steps_calculation = 0  # Increase for each simulation step
        self.steps_output = 0
        self.elements_deactivated = self.ElementType()  # Empty array
        self.elements = self.ElementType()  # Empty array

        if loglevel != 'custom':
            logging.getLogger('').handlers = []
            logging.basicConfig(level=loglevel,
                                format='%(levelname)s: %(message)s')

        # Prepare outfile
        try:
            io_module = __import__('opendrift.export.io_' + iomodule,
                                   fromlist=['init', 'write_buffer',
                                             'close', 'import_file'])
        except ImportError:
            logging.info('Could not import iomodule ' + iomodule)
        self.io_init = types.MethodType(io_module.init, self)
        self.io_write_buffer = types.MethodType(io_module.write_buffer, self)
        self.io_close = types.MethodType(io_module.close, self)
        self.io_import_file = types.MethodType(io_module.import_file, self)

        self.timer_start('total time')
        self.timer_start('configuration')

        logging.info('OpenDriftSimulation initialised')

    def _add_config(self, key, value, comment, overwrite=False):
        """Add a configuration item to the current model."""
        cs = ''  # configstring
        for i, s in enumerate(key.split(':')):
            if i < len(key.split(':')) - 1:
                cs += '\n' + '['*(i+1) + s + ']'*(i+1)
            else:
                cs += '\n\t' + s + ' = ' + value
        newconf = configobj.ConfigObj(configspec=cs.split('\n'))
        newconf.validate(validate.Validator())
        if not hasattr(self, 'configobj'):
            self.configobj = newconf
        else:
            self._merge_config(newconf, overwrite=True)

    def _merge_config(self, newconf, overwrite=False):
        if overwrite is True:
            self.configobj.merge(newconf)
            self.configobj.configspec.merge(newconf.configspec)
        else:
            newconf.configspec.merge(self.configobj.configspec)
            self.configobj.configspec = newconf.configspec
            newconf.merge(self.configobj)
            self.configobj = newconf
        
    def _add_configstring(self, configstring):
        """Add several configuration items from string (INI-format)."""
        newconf = configobj.ConfigObj(configspec=configstring.split('\n'))
        newconf.validate(validate.Validator())
        if not hasattr(self, 'configobj'):
            self.configobj = newconf
        else:
            self.configobj.merge(newconf)
            self.configobj.configspec.merge(newconf.configspec)

    def set_config(self, key, value):
        """Provide a value for a configuration setting"""
        d = self.configobj
        ds = self.configobj.configspec
        for i, s in enumerate(key.split(':')):
            if s not in d:
                self.list_configspec()
                raise ValueError('Wrong configuration')
            if not isinstance(d[s], dict):
                d[s] = value
            else:
                d = d[s]
                ds = ds[s]
        valid = self.configobj.validate(validate.Validator())
        if self.configobj.validate(validate.Validator()) is not True:
            raise ValueError('Wrong configuration:\n\t%s = %s' % (s, ds[s]))

    def _config_hash_to_dict(self, hashstring):
        """Return configobj-dict from section:section:key format"""
        c = self.configobj
        cs = self.configobj.configspec
        for i, s in enumerate(hashstring.split(':')):
            if isinstance(c[s], dict):
                c = c[s]
                cs = cs[s] 
        return c, cs, s

    def get_config(self, key):
        """Get value of a configuration setting"""
        c, cs, s = self._config_hash_to_dict(key)
        return c[s]

    def get_configspec(self, key):
        """Get configuration specification of a configuration setting"""
        c, cs, s = self._config_hash_to_dict(key)
        return cs[s]

    def _config_hashstrings(self):
        """Get list of strings section:section:key for all config items"""
        def fun(k, d, pre):
            path = '%s:%s' % (pre, k) if pre else k
            return path if type(d[k]) is not configobj.Section else \
                ",".join([fun(i,d[k], path) for i in d[k]])
        return ",".join([fun(k,self.configobj, '') for
                         k in self.configobj]).split(',')

    def list_configspec(self):
        """List all possible configuration settings with specifications"""
        keys = self._config_hashstrings()
        str = '\n==============================================\n'
        for key in keys:
            str += '%s [%s] %s\n' % (key, self.get_config(key),
                                   self.get_configspec(key))
        str += '==============================================\n'
        logging.info(str)

    def list_config(self):
        """List all possible configuration settings with values"""
        keys = self._config_hashstrings()
        str = '\n=============================================\n'
        for key in keys:
            str += '%s [%s]\n' % (key, self.get_config(key))
        str += '=============================================\n'
        logging.info(str)

    def prepare_run(self):
        pass  # to be overloaded when needed

    def store_present_positions(self):
        """Store present element positions, in case they shall be moved back"""
        if self.get_config('general:coastline_action') == 'previous':
            if not hasattr(self, 'previous_lon'):
                self.previous_lon = np.ma.masked_all(self.num_elements_total())
                self.previous_lat = np.ma.masked_all(self.num_elements_total())
            self.previous_lon[self.elements.ID-1] = np.copy(self.elements.lon)
            self.previous_lat[self.elements.ID-1] = np.copy(self.elements.lat)

    def previous_position_if(self):
        return None

    def interact_with_coastline(self):
        """Coastline interaction according to configuration setting"""
        i = self.get_config('general:coastline_action')
        if not hasattr(self.environment, 'land_binary_mask'):
            return
        if i == 'none':  # Do nothing
            return
        if i == 'stranding':  # Deactivate elements on land
            self.deactivate_elements(
                self.environment.land_binary_mask == 1, reason='stranded')
        elif i == 'previous':  # Go back to previous position (in water)
            previous_position_if = self.previous_position_if()
            if not hasattr(self, 'previous_lon'):
                self.deactivate_elements(
                    self.environment.land_binary_mask == 1,
                    reason='seeded_on_land')
                if previous_position_if is not None:
                    self.deactivate_elements((previous_position_if*1 == 1) & (
                                     self.environment.land_binary_mask == 0),
                                         reason='seeded_at_nodata_position')
                return

            if previous_position_if is None:
                on_land = np.where(self.environment.land_binary_mask == 1)[0]
            else:
                on_land = np.where((self.environment.land_binary_mask == 1) |
                                   (previous_position_if == 1))[0]
            if len(on_land) == 0:
                logging.debug('No elements hit coastline.')
            else:
                logging.debug('%s elements hit coastline, '
                              'moving back to water' % len(on_land))
                on_land_ID = self.elements.ID[on_land] 
                self.elements.lon[on_land] = \
                    np.copy(self.previous_lon[on_land_ID - 1])
                self.elements.lat[on_land] = \
                    np.copy(self.previous_lat[on_land_ID - 1])
                self.environment.land_binary_mask[on_land] = 0

    @abstractmethod
    def update(self):
        """Any trajectory model implementation must define an update method.
        This method must/can use environment data (self.environment) to
        update properties (including position) of its particles (self.elements)
        """

    @abstractproperty
    def ElementType(self):
        """Any trajectory model implementation must define an ElementType."""

    @abstractproperty
    def required_variables(self):
        """Any trajectory model implementation must list needed variables."""

    def test_data_folder(self):
        import opendrift
        return os.path.abspath(
            os.path.join(os.path.dirname(opendrift.__file__),
                         '..', 'tests', 'test_data')) + os.path.sep

    def set_projection(self, proj4):
        """Set the projection onto which data from readers is reprojected."""
        self.proj4 = proj4
        if proj4 is not None:
            self.proj = pyproj.Proj(self.proj4 + ' +ellps=WGS84')
            logging.info('Calculation SRS set to: ' + self.proj.srs)
        else:
            self.proj = None
            logging.info('Calculation SRS set to: ' + str(self.proj))

    def lonlat2xy(self, lon, lat):
        """Calculate x,y in own projection from given lon,lat (scalars/arrays).
        """
        if self.proj.is_latlong():
            return lon, lat
        else:
            x, y = self.proj(lon, lat, inverse=False)
            if 'ob_tran' in self.proj4:
                # NB: should check if ob_tran is sufficient condition;
                # may need lonlat as well?
                return np.degrees(x), np.degrees(y)
            else:
                return x, y

    def xy2lonlat(self, x, y):
        """Calculate lon,lat from given x,y (scalars/arrays) in own projection.
        """
        if self.proj.is_latlong():
            return x, y
        else:
            if 'ob_tran' in self.proj4:
                logging.info('NB: Converting deg to rad due to ob_tran srs')
                x = np.radians(np.array(x))
                y = np.radians(np.array(y))
            return self.proj(x, y, inverse=True)

    def timer_start(self, category):
        if not hasattr(self, 'timers'):
            self.timers = OrderedDict()
        if not hasattr(self, 'timing'):
            self.timing = OrderedDict()
        if category not in self.timing:
            self.timing[category] = timedelta(0)
        self.timers[category] = datetime.now()

    def timer_end(self, category):
        if self.timers[category] is not None:
            self.timing[category] += datetime.now() - self.timers[category]
        self.timers[category] = None

    def performance(self):
        '''Report the time spent on various tasks'''

        outStr = '---------------------------\n'
        outStr += 'Performance:\n'
        for category, time in self.timing.iteritems():
            timestr = str(time)[0:str(time).find('.') + 2]
            for i, c in enumerate(timestr):
                if c in '123456789.':
                    timestr = timestr[i:]  # Strip leading 0 and :
                    if c == '.':
                        timestr = '0' + timestr
                    break
            parts = category.split(':')
            indent = '  '*(len(parts) - 1)
            category = parts[-1]
            category = category.replace('<colon>', ':')
            outStr += '%s%7s %s\n' % (indent, timestr, category)
        return outStr

    def add_reader(self, readers, variables=None):
        """Add one or more readers providing variables used by this model.

        Method may be called subsequently to add more readers
        for other variables.

        Args:
            readers: one or more (list) Reader objects.
            variables: optional, list of strings of standard_name of
                variables to be provided by this/these reader(s).
        """

        # Convert any strings to lists, for looping
        if isinstance(variables, str):
            variables = [variables]
        if isinstance(readers, BaseReader):
            readers = [readers]

        for reader in readers:
            # Check if input class is of correct type
            if not isinstance(reader, BaseReader):
                raise TypeError('Please provide Reader object')

            # Check that reader class contains the requested variables
            if variables is not None:
                missingVariables = set(variables) - set(reader.variables)
                if missingVariables:
                    raise ValueError('Reader %s does not provide variables: %s'
                                     % (reader.name, list(missingVariables)))

            # Finally add new reader to list
            if reader.name in self.readers:
                # Reader names must be unique, adding integer
                for n in range(99999):
                    tmp_name = reader.name + '_%d' % n
                    if tmp_name not in self.readers:
                        reader.name = tmp_name
                        break

            # Horizontal buffer of reader must be large enough to cover
            # the distance possibly covered by elements within a time step
            reader.set_buffer_size(max_speed=self.max_speed)

            self.readers[reader.name] = reader
            if self.proj is None:
                if reader.proj4 is not None and reader.proj4 != 'None':
                    self.set_projection(reader.proj4)
                    logging.debug('Using srs for common grid: %s' %
                                  self.proj4)
                else:
                    logging.debug('%s is unprojected, cannot use '
                                  'for common grid' % reader.name)
            logging.debug('Added reader ' + reader.name)

            # Add this reader for each of the given variables
            for variable in variables if variables else reader.variables:
                if variable in self.priority_list:
                    if reader.name not in self.priority_list[variable]:
                        self.priority_list[variable].append(reader.name)
                else:
                    self.priority_list[variable] = [reader.name]

        # Remove/hide variables not needed by the current trajectory model
        for variable in self.priority_list:
            if variable not in self.required_variables:
                del self.priority_list[variable]

        # Set projection to latlong if not taken from any of the readers
        if self.proj is None:
            logging.info('Setting SRS to latlong, since not defined before.')
            self.set_projection('+proj=latlong')

    def add_readers_from_list(self, urls, timeout=10):
        '''Make readers from a list of URLs or paths to netCDF datasets'''

        from opendrift.readers.reader_netCDF_CF_generic import Reader
        for u in urls:
            files = glob.glob(u)
            for f in files:  # Regular file
                try:
                    r = Reader(f)
                    self.add_reader(r)
                except:
                    logging.warning('%s is not a netCDF file recognised by '
                                    'OpenDrift' % f)
            if files == []:  # Try with OPeNDAP URL
                try:
                    import urllib2
                    urllib2.urlopen(u, timeout=timeout)
                except Exception as e:
                    # Error code 400 is expected!
                    if not isinstance(e, urllib2.HTTPError) or e.code != 400:
                        logging.warning('ULR %s not accessible: ' % u + str(e))
                        continue
                    try:
                        r = Reader(u)
                        self.add_reader(r)
                    except Exception as e:
                        logging.warning('%s is not a netCDF file recognised '
                                        'by OpenDrift: %s' % (u, str(e)))

    def list_environment_variables(self):
        """Return list of all variables provided by the added readers."""
        variables = []
        for reader in self.readers:
            variables.extend(self.readers[reader].variables)
        return variables

    def missing_variables(self):
        """Return list of all variables for which no reader has been added."""
        return [var for var in self.required_variables
                if var not in self.priority_list]

    def get_reader_groups(self, variables=None):
        """Find which groups of variables are provided by the same readers.

        This function loops through 'priority_list' (see above) and groups
        all variables returned by the same readers in the same order. This
        allows asking readers for several variables simultaneously,
        improving performance. Used by method 'get_environment'.

        Returns:
            variable_groups: list of lists of (environment) variables.
            reader_groups: list of list of reader names, corresponding to
                each of the variable_groups.
        """
        if variables is None:
            variables = self.required_variables
        reader_groups = []
        # Find all unique reader groups
        for variable, readers in self.priority_list.items():
            if (variable in variables) and (readers not in reader_groups):
                reader_groups.append(readers)
        # Find all variables returned by the same reader group
        variable_groups = [None]*len(reader_groups)
        for variable, readers in self.priority_list.items():
            if variable not in variables:
                continue
            for i, readerGroup in enumerate(reader_groups):
                if readers == readerGroup:
                    if variable_groups[i]:
                        variable_groups[i].append(variable)
                    else:
                        variable_groups[i] = [variable]

        missing_variables = list(set(variables) -
                                 set(self.priority_list.keys()))

        return variable_groups, reader_groups, missing_variables

    def get_environment(self, variables, time, lon, lat, z, profiles):
        '''Retrieve environmental variables at requested positions.

        Updates:
            Buffer (raw data blocks) for each reader stored for performance:
                [readers].var_block_before (last before requested time)
                [readers].var_block_after (first after requested time)
                    - lists of one ReaderBlock per variable group:
                        - time, x, y, [vars]
        Returns:
            environment: recarray with variables as named attributes,
                         interpolated to requested positions/time.

        '''
        self.timer_start('main loop:readers')
        # Initialise ndarray to hold environment variables
        dtype = [(var, np.float32) for var in variables]
        env = np.ma.array(np.zeros(len(lon)), dtype=dtype)

        # For each variable/reader group:
        variable_groups, reader_groups, missing_variables = \
            self.get_reader_groups(variables)
        for variable in variables:  # Fill with fallback value if no reader
            if (self.fallback_values is not None
                    and variable in self.fallback_values):
                env[variable] = np.ma.ones(env[variable].shape)\
                    * self.fallback_values[variable]

        for i, variable_group in enumerate(variable_groups):
            logging.debug('----------------------------------------')
            logging.debug('Variable group %s' % (str(variable_group)))
            logging.debug('----------------------------------------')
            reader_group = reader_groups[i]
            missing_indices = np.array(range(len(lon)))
            # For each reader:
            for reader_name in reader_group:
                logging.debug('Calling reader ' + reader_name)
                logging.debug('----------------------------------------')
                self.timer_start('main loop:readers:' +
                                 reader_name.replace(':', '<colon>'))
                reader = self.readers[reader_name]
                if not reader.covers_time(time):
                    logging.debug('\tOutside time coverage of reader.')
                    continue
                # Fetch given variables at given positions from current reader
                try:
                    logging.debug('Data needed for %i elements' %
                                  len(missing_indices))
                    # Check if vertical profiles are requested from reader
                    if profiles is not None:
                        profiles_from_reader = list(
                            set(variable_group) & set(profiles))
                        if profiles_from_reader == []:
                            profiles_from_reader = None
                    else:
                        profiles_from_reader = None
                    env_tmp, env_profiles_tmp = \
                        reader.get_variables_interpolated(
                            variable_group, profiles_from_reader,
                            self.required_profiles_z_range, time,
                            lon[missing_indices], lat[missing_indices],
                            z[missing_indices], self.use_block, self.proj)

                except Exception as e:
                    logging.info('========================')
                    logging.info('Exception:')
                    logging.info(e)
                    logging.debug(traceback.format_exc())
                    logging.info('========================')
                    self.timer_end('main loop:readers:' +
                                   reader_name.replace(':', '<colon>'))
                    continue

                # Copy retrieved variables to env array, and mask nan-values
                for var in variable_group:
                    env[var][missing_indices] = np.ma.masked_invalid(
                        env_tmp[var]).astype('float32')
                    if profiles_from_reader is not None and var in profiles_from_reader:
                        if 'env_profiles' not in locals():
                            env_profiles = env_profiles_tmp
                        # TODO: fix to be checked
                        if var in env_profiles and var in env_profiles_tmp:
                            # If one profile has fewer vertical layers than
                            # the other, we use only the overlapping part
                            if len(env_profiles['z']) != len(
                                env_profiles_tmp['z']):
                                logging.debug('Warning: different number of '
                                    ' vertical layers: %s and %s' % (
                                        len(env_profiles['z']),
                                        len( env_profiles_tmp['z'])))
                            z_ind = np.arange(np.minimum(
                                len(env_profiles['z'])-1,
                                len(env_profiles_tmp['z'])-1))
                            env_profiles[var][np.ix_(z_ind, missing_indices)] = \
                                np.ma.masked_invalid(env_profiles_tmp[var][z_ind,:]).astype('float32')

                # Detect elements with missing data, for present reader group
                if hasattr(env_tmp[variable_group[0]], 'mask'):
                    try:
                        del combined_mask
                    except:
                        pass
                    for var in variable_group:
                        tmp_var = np.ma.masked_invalid(env_tmp[var])
                        # Changed 13 Oct 2016, but uncertain of effect
                        # TODO: to be checked 
                        #tmp_var = env_tmp[var]
                        if 'combined_mask' not in locals():
                            combined_mask = np.ma.getmask(tmp_var)
                        else:
                            combined_mask = \
                                np.ma.mask_or(combined_mask,
                                              np.ma.getmask(tmp_var),
                                              shrink=False)
                    missing_indices = missing_indices[combined_mask]
                else:
                    missing_indices = []  # temporary workaround
                if (type(missing_indices) == np.int64) or (
                        type(missing_indices) == np.int32):
                    missing_indices = []
                self.timer_end('main loop:readers:' +
                               reader_name.replace(':', '<colon>'))
                if len(missing_indices) == 0:
                    logging.debug('Obtained data for all elements.')
                    break
                else:
                    logging.debug('Data missing for %i elements.' %
                                  (len(missing_indices)))

        logging.debug('---------------------------------------')
        logging.debug('Finished processing all variable groups')

        for var in self.fallback_values:
            if (var not in variables) and (profiles is None or var not in profiles):
                continue
            mask = env[var].mask
            if sum(mask==True) > 0:
                logging.debug('    Using fallback value %s for %s for %s elements' %
                              (self.fallback_values[var], var, sum(mask==True)))
                env[var][mask] = self.fallback_values[var]
            # Profiles
            if profiles is not None and var in profiles:
                if 'env_profiles' not in locals():
                    logging.debug('Creating empty dictionary for profiles not '
                                  'profided by any reader: ' + str(self.required_profiles))
                    env_profiles = {}
                    env_profiles['z'] = \
                        np.array(self.required_profiles_z_range)[::-1]
                if var not in env_profiles:
                    logging.debug('      Using fallback value %s for %s for all profiles' %
                                  (self.fallback_values[var], var))
                    env_profiles[var] = self.fallback_values[var]*\
                        np.ma.ones((len(env_profiles['z']), self.num_elements_active()))
                else:
                    mask = env_profiles[var].mask
                    num_masked_values_per_element = sum(mask==True)
                    num_missing_profiles = sum(num_masked_values_per_element == len(env_profiles['z']))
                    env_profiles[var][mask] = self.fallback_values[var]
                    logging.debug('      Using fallback value %s for %s for %s profiles' %
                                  (self.fallback_values[var], var, num_missing_profiles,))
                    num_missing_individual = sum(num_masked_values_per_element > 0) - num_missing_profiles
                    if num_missing_individual > 0:
                        logging.debug('        ...plus %s individual points in other profiles' %
                                      num_missing_individual)
       
        #####################
        # Diagnostic output
        #####################
        if len(env) > 0:
            logging.debug('------------ SUMMARY -------------')
            for var in variables:
                logging.debug('    %s: %g (min) %g (max)' %
                              (var, env[var].min(), env[var].max()))
            logging.debug('---------------------------------')
            logging.debug('\t\t%s active elements' % self.num_elements_active())
            if self.num_elements_active() > 0:
                lonmin = self.elements.lon.min()
                lonmax = self.elements.lon.max()
                latmin = self.elements.lat.min()
                latmax = self.elements.lat.max()
                zmin = self.elements.z.min()
                zmax = self.elements.z.max()
                if latmin == latmax:
                    logging.debug('\t\tlatitude =  %s' % (latmin))
                else:
                    logging.debug('\t\t%s <- latitude  -> %s' % (latmin, latmax))
                if lonmin == lonmax:
                    logging.debug('\t\tlongitude = %s' % (lonmin))
                else:
                    logging.debug('\t\t%s <- longitude -> %s' % (lonmin, lonmax))
                if zmin == zmax:
                    logging.debug('\t\tz = %s' % (zmin))
                else:
                    logging.debug('\t\t%s   <- z ->   %s' % (zmin, zmax))
                logging.debug('---------------------------------')

        # Prepare array indiciating which elements contain any invalid values
        missing = np.ma.masked_invalid(env[variables[0]]).mask
        for var in variables[1:]:
            missing = np.ma.mask_or(missing,
                                    np.ma.masked_invalid(env[var]).mask,
                                    shrink=False)

        # Convert dictionary to recarray and return
        if 'env_profiles' not in locals():
            env_profiles = None

        # Convert masked arrays to regular arrays for increased performance
        env = np.array(env)
        if env_profiles is not None:
            for var in env_profiles:
                env_profiles[var] = np.array(env_profiles[var])

        self.timer_end('main loop:readers')

        return env.view(np.recarray), env_profiles, missing

    def num_elements_active(self):
        """The number of active elements."""
        if hasattr(self, 'elements'):
            return len(self.elements)
        else:
            return 0

    def num_elements_deactivated(self):
        """The number of deactivated elements."""
        if hasattr(self, 'elements_deactivated'):
            return len(self.elements_deactivated)
        else:
            return 0

    def num_elements_scheduled(self):
        if hasattr(self, 'elements_scheduled'):
            return len(self.elements_scheduled)
        else:
            return 0

    def num_elements_total(self):
        """The total number of scheduled, active and deactivated elements."""
        return self.num_elements_activated() + self.num_elements_scheduled()

    def num_elements_activated(self):
        """The total number of active and deactivated elements."""
        return self.num_elements_active() + self.num_elements_deactivated()

    def schedule_elements(self, elements, time):
        """Schedule elements to be seeded during runtime.

        Also assigns a unique ID to each particle, monotonically increasing."""

        # prepare time
        if type(time) == datetime:
            time = [time]*len(elements)  # Convert to array of same length
        if not hasattr(self, 'elements_scheduled'):
            self.elements_scheduled = elements
            self.elements_scheduled_time = np.array(time)
            # We start simulation at time of release of first element:
            self.start_time = time[0]
            self.elements_scheduled.ID = np.arange(1, len(elements) + 1)
        else:
            elements.ID = np.arange(self.num_elements_scheduled() + 1,
                                    self.num_elements_scheduled() + 1 +
                                    len(elements))  # Increase ID successively
            self.elements_scheduled.extend(elements)
            self.elements_scheduled_time = np.append(
                self.elements_scheduled_time, np.array(time))

        min_time = np.min(time)
        if hasattr(self, 'start_time'):
            if min_time < self.start_time:
                self.start_time = min_time
                logging.debug('Setting simulation start time to %s' %
                              str(min_time))
        else:
            self.start_time = min_time
            logging.debug('Setting simulation start time to %s' %
                          str(min_time))

    def release_elements(self):
        """Activate elements which are scheduled within following timestep."""

        logging.debug('to be seeded: %s, already seeded %s' % (
            len(self.elements_scheduled), self.num_elements_activated()))
        if len(self.elements_scheduled) == 0:
            return
        if self.time_step.days >= 0:
            indices = (self.elements_scheduled_time >= self.time) & \
                      (self.elements_scheduled_time <
                       self.time + self.time_step)
        else:
            indices = (self.elements_scheduled_time <= self.time) & \
                      (self.elements_scheduled_time >
                       self.time + self.time_step)
        self.elements_scheduled.move_elements(self.elements, indices)
        self.elements_scheduled_time = self.elements_scheduled_time[~indices]
        logging.debug('Released %i new elements.' % np.sum(indices))

    def seed_elements(self, lon, lat, radius=0, number=None, time=None,
                      cone=False, **kwargs):
        """Seed a given number of particles around given position(s).

        Arguments:
            lon: scalar or array, central longitude(s).
            lat: scalar or array, central latitude(s).
            radius: scalar or array, radius in meters around each lon-lat
                pair, within which particles will be randomly seeded.
            number: integer, total number of particles to be seeded
                Elements are spread equally among the given lon/lat points.
                Default is one particle for each lon-lat pair.
            time: datenum, the time at which particles are seeded/released.
                If time is an array with two elements, elements are seeded
                continously from start/first to end/last time.
            cone: boolean or integer. If True, lon and lat must be two element
                arrays, interpreted as the start and end position of a cone
                within which elements will be seeded. Radius may also be a
                two element array specifying the radius around the points.
            kwargs: keyword arguments containing properties/attributes and
                values corresponding to the actual particle type (ElementType).
                These are forwarded to the ElementType class. All properties
                for which there are no default value must be specified.
        """

        if time is None:
            raise ValueError('Time of seeding must be specified')

        #################################################################
        # Make arrays of all input parameters, with one element per
        # lon/lat pair, for sequential seeding
        #################################################################
        lon = np.atleast_1d(lon)
        lat = np.atleast_1d(lat)
        num_points = len(lon)  # Number of lon/lat pairs
        if number is not None and number < num_points:
            raise ValueError('Number of elements must be greater or equal '
                             'to number of points.')

        if num_points == 1 and number is None:
            number = 1

        if num_points > 1:
            ###############################
            # lon and lat are arrays
            ###############################
            radius_array = np.atleast_1d(radius)
            if len(radius_array) == 1:
                # If scalar radius is given, apply to all points
                radius_array = radius_array*np.ones(num_points)
            if number is None:
                # Default is one element per given position
                #number = 1*np.ones(num_points)
                number = num_points  # temporarily scalar, should be array
            number = np.atleast_1d(number)
            if len(number) == 1 and num_points > 1:
                number_array = np.floor(number/num_points)*np.ones(num_points)
                # Add remaining elements to first point
                remainder = number - np.floor(number/num_points)*num_points
                number_array[0] = number_array[0] + remainder

            if isinstance(time, datetime):
                time = [time, time]

            if type(time) == list and len(time) == 2:
                td = (time[1]-time[0])/(number-1)  # timestep between points
                if len(td) == 1:
                    td = td[0]
                time_array = [time[0] + i*td for i in range(number)]
                indx_time_end = np.cumsum(number_array, dtype=int)
                indx_time_start = np.append([0], indx_time_end[0:-1])
                time_array2 = [time_array[int(indx_time_start[i]):
                                          int(indx_time_end[i])]
                               for i in range(num_points)]
                time_array = time_array2  # Subset of times for this point

            if cone is True:
                ###################################################
                # lon and lat are start and end points of a cone
                ###################################################
                if len(lon) != 2 or len(lat) != 2:
                    raise ValueError('When cone is True, lon and lat must '
                                     'be 2-element arrays.')

                geod = pyproj.Geod(ellps='WGS84')
                conelonlats = geod.npts(lon[0], lat[0], lon[1], lat[1],
                                        number, radians=False)
                # Seed cone recursively
                lon, lat = zip(*conelonlats)
                lon = np.atleast_1d(lon)
                lat = np.atleast_1d(lat)
                if len(radius_array) == 1:
                    radius_array = [radius, radius]
                radius_array = np.linspace(radius_array[0], radius_array[1],
                                           number)
                number_array = np.ones(number)
                time_array = [time[0] + i*td for i in range(number)]

            if 'z' in kwargs and kwargs['z'] == 'seafloor':
                # We need to fetch seafloor depth from reader
                if 'sea_floor_depth_below_sea_level' not in self.priority_list:
                    raise ValueError('A reader providing the variable '
                                     'sea_floor_depth_below_sea_level must be '
                                     'added before seeding elements at seafloor.')
                if type(time) is list:
                    t = time[0]
                else:
                    t = time
                env, env_profiles, missing = \
                    self.get_environment(['sea_floor_depth_below_sea_level'],
                                         time=t, lon=lon, lat=lat,
                                         z=0*lon, profiles=None)
                kwargs['z'] = \
                    -env['sea_floor_depth_below_sea_level'].astype('float32')

            # Recursively seeding elements around each point
            scalarargs = {}
            for i in range(len(lon)):
                for kwarg in kwargs:
                    try:
                        scalarargs[kwarg] = kwargs[kwarg][i]
                    except:
                        scalarargs[kwarg] = kwargs[kwarg]
                # Make sure to call seed function of base class,
                # not of a specific Children class
                OpenDriftSimulation.seed_elements(
                    self, lon=[lon[i]], lat=[lat[i]], radius=radius_array[i],
                    number=int(number_array[i]),
                    time=time_array[i], cone=False, **scalarargs)
            return

        # Below we have only for single points
        if isinstance(time, datetime) and number > 1:
            time = [time, time]

        if type(time) == list and len(time) == 2 and number > 1:
            td = (time[1]-time[0])/(number-1)  # timestep between points
            # Spread seeding times equally between start/first and end/last
            time_array = [time[0] + i*td for i in range(number)]
        else:
            time_array = time

        geod = pyproj.Geod(ellps='WGS84')
        ones = np.ones(number)
        x = np.random.randn(number)*radius
        y = np.random.randn(number)*radius
        az = np.degrees(np.arctan2(x, y))
        dist = np.sqrt(x*x+y*y)
        kwargs['lon'], kwargs['lat'], az = \
            geod.fwd(lon*ones, lat*ones, az, dist, radians=False)

        if 'z' in kwargs and kwargs['z'] == 'seafloor':
            # We need to fetch seafloor depth from reader
            # Unfortunately, this is duplication of code above
            if 'sea_floor_depth_below_sea_level' not in self.priority_list:
                raise ValueError('A reader providing the variable '
                                 'sea_floor_depth_below_sea_level must be '
                                 'added before seeding elements at seafloor.')
            if type(time) is list:
                t = time[0]
            else:
                t = time
            env, env_profiles, missing = \
                self.get_environment(['sea_floor_depth_below_sea_level'],
                                     t, kwargs['lon'], kwargs['lat'],
                                     0.*ones, None)
            kwargs['z'] = \
                -env['sea_floor_depth_below_sea_level'].astype('float32')

        elements = self.ElementType(**kwargs)

        self.schedule_elements(elements, time_array)

    def seed_within_polygon(self, lons, lats, number, **kwargs):
        """Seed a number of elements within given polygon.

        Arguments:
            lon: array of longitudes
            lat: array of latitudes
            number: int, number of elements to be seeded
            kwargs: keyword arguments containing properties/attributes and
                values corresponding to the actual particle type (ElementType).
                These are forwarded to method seed_elements(). All properties
                for which there are no default value must be specified.

        """
        if number == 0:
            return

        lons = np.asarray(lons)
        lats = np.asarray(lats)
        if len(lons) < 3:
            logging.info('At least three points needed to make a polygon')
            return
        if len(lons) != len(lats):
            raise ValueError('lon and lat arrays must have same length.')
        poly = Polygon(zip(lons, lats), closed=True)
        # Place N points within the polygons
        proj = pyproj.Proj('+proj=aea +lat_1=%f +lat_2=%f +lat_0=%f '
                           '+lon_0=%f +R=6370997.0 +units=m +ellps=WGS84'
                           % (lats.min(), lats.max(),
                              (lats.min()+lats.max())/2,
                              (lons.min()+lons.max())/2))
        lonlat = poly.get_xy()
        lon = lonlat[:, 0]
        lat = lonlat[:, 1]
        x, y = proj(lon, lat)
        area = 0.0
        for i in xrange(-1, len(x)-1):
            area += x[i] * (y[i+1] - y[i-1])
        area = abs(area) / 2

        # Make points, evenly distributed
        deltax = np.sqrt(area/number)
        lonpoints = np.array([])
        latpoints = np.array([])
        lonlat = poly.get_xy()
        lon = lonlat[:, 0]
        lat = lonlat[:, 1]
        x, y = proj(lon, lat)
        xvec = np.linspace(x.min() + deltax/2, x.max() - deltax/2,
                           int((x.max()-x.min())/deltax))
        yvec = np.linspace(y.min() + deltax/2, y.max() - deltax/2,
                           int((y.max()-y.min())/deltax))
        x, y = np.meshgrid(xvec, yvec)
        lon, lat = proj(x, y, inverse=True)
        lon = lon.ravel()
        lat = lat.ravel()
        points = np.c_[lon, lat]
        if have_nx:
            ind = nx.points_inside_poly(points, poly.xy)
        else:
            ind = Path(poly.xy).contains_points(points)
        lonpoints = np.append(lonpoints, lon[ind])
        latpoints = np.append(latpoints, lat[ind])
        if len(ind) == 0:
            logging.info('Small or irregular polygon, using center point.')
            lonpoints = np.atleast_1d(np.mean(lons))
            latpoints = np.atleast_1d(np.mean(lats))
        # Truncate if too many
        # NB: should also repeat some points, if too few
        lonpoints = lonpoints[0:number]
        latpoints = latpoints[0:number]
        if len(lonpoints) < number:
            # If number of positions is smaller than requested,
            # we duplicate the first ones
            missing = number - len(lonpoints)
            lonpoints = np.append(lonpoints, lonpoints[0:missing])
            latpoints = np.append(latpoints, latpoints[0:missing])

        # Finally seed at calculated positions
        self.seed_elements(lonpoints, latpoints, **kwargs)

    def seed_from_shapefile(self, shapefile, number,
                            layername=None, featurenum=None, **kwargs):
        """Seeds elements within contours read from a shapefile"""

        try:
            import ogr
            import osr
        except Exception as e:
            print e
            raise ValueError('OGR library is needed to read shapefiles.')

        if 'timeformat' in kwargs:
            # Recondstructing time from filename, where 'timeformat'
            # is forwarded to datetime.strptime()
            kwargs['time'] = datetime.strptime(os.path.basename(shapefile),
                                               kwargs['timeformat'])
            del kwargs['timeformat']

        targetSRS = osr.SpatialReference()
        targetSRS.ImportFromEPSG(4326)
        s = ogr.Open(shapefile)

        for layer in s:
            if layername is not None and layer.GetName() != layername:
                logging.info('Skipping layer: ' + layer.GetName())
                continue
            else:
                logging.info('Seeding for layer: %s (%s features)' %
                             (layer.GetDescription(), layer.GetFeatureCount()))

            coordTrans = osr.CoordinateTransformation(layer.GetSpatialRef(),
                                                      targetSRS)

            if featurenum is None:
                featurenum = range(1, layer.GetFeatureCount() + 1)
            else:
                featurenum = np.atleast_1d(featurenum)
            if max(featurenum) > layer.GetFeatureCount():
                raise ValueError('Only %s features in layer.' %
                                 layer.GetFeatureCount())

            # Loop first through all features to determine total area
            total_area = 0
            for f in featurenum:
                feature = layer.GetFeature(f - 1)  # Note 1-indexing, not 0
                total_area += feature.GetGeometryRef().GetArea()
            layer.ResetReading()  # Rewind to first layer
            logging.info('Total area of all polygons: %s m2' % total_area)

            num_seeded = 0
            for i, f in enumerate(featurenum):
                feature = layer.GetFeature(f - 1)
                geom = feature.GetGeometryRef()
                num_elements = np.int(number*geom.GetArea()/total_area)
                if f == featurenum[-1]:
                    # For the last feature we seed the remaining number,
                    # avoiding difference due to rounding:
                    num_elements = number - num_seeded
                logging.info('\tSeeding %s elements within polygon number %s' %
                             (num_elements, featurenum[i]))
                geom.Transform(coordTrans)
                b = geom.GetBoundary()
                if b is not None:
                    points = b.GetPoints()
                    lons = [p[0] for p in points]
                    lats = [p[1] for p in points]
                else:
                    # Alternative if OGR is not built with GEOS support
                    r = geom.GetGeometryRef(0)
                    lons = [r.GetX(j) for j in xrange(r.GetPointCount())]
                    lats = [r.GetY(j) for j in xrange(r.GetPointCount())]

                self.seed_within_polygon(lons, lats, num_elements, **kwargs)
                num_seeded += num_elements

    def deactivate_elements(self, indices, reason='deactivated'):
        """Schedule deactivated particles for deletion (at end of step)"""
        if sum(indices) == 0:
            return
        if reason not in self.status_categories:
            self.status_categories.append(reason)
            logging.debug('Added status %s' % (reason))
        reason_number = self.status_categories.index(reason)
        #if not hasattr(self.elements.status, "__len__"):
        if len(np.atleast_1d(self.elements.status)) == 1:
            status = np.asscalar(self.elements.status)
            self.elements.status = np.zeros(self.num_elements_active())
            self.elements.status.fill(status)
        self.elements.status[indices] = reason_number
        logging.debug('%s elements scheduled for deactivation (%s)' %
                      (sum(indices), reason))

    def remove_deactivated_elements(self):
        """Moving deactivated elements from self.elements
        to self.elements_deactivated."""

        # All particles scheduled for deletion
        indices = (self.elements.status != 0)
        #try:
        #    len(indices)
        #except:
        if indices == [] or len(indices) == 0 or sum(indices) == 0:
            logging.debug('No elements to deactivate')
            return  # No elements scheduled for deactivation
        # Basic, but some more housekeeping will be required later
        self.elements.move_elements(self.elements_deactivated, indices)
        logging.debug('Removed %i elements.' % (sum(indices)))
        if hasattr(self, 'environment'):
            self.environment = self.environment[~indices]
            logging.debug('Removed %i values from environment.' %
                          (sum(indices)))
        if hasattr(self, 'environment_profiles') and \
                self.environment_profiles is not None:
            for varname, profiles in self.environment_profiles.iteritems():
                logging.debug('remove items from profile for '+varname)
                if varname is not 'z':
                    self.environment_profiles[varname] = \
                        profiles[:, ~indices]
            logging.debug('Removed %i values from environment_profiles.' %
                          (sum(indices)))
            #if self.num_elements_active() == 0:
            #    raise ValueError('No more active elements.')  # End simulation

    def run(self, time_step=3600, steps=None, time_step_output=None,
            duration=None, end_time=None, outfile=None, export_variables=None,
            export_buffer_length=100, stop_on_error=False):
        """Start a trajectory simulation, after initial configuration.

        Performs the main loop:
            - Obtain environment data for positions of all particles.
            - Call method 'update' to update (incl advect) particle properties.
        until one of the following conditions are met:
            - Maximum number of steps are reached
            - A needed variable can not be obtained by any reader
                (outside spatial/temporal domain) and has no fallback
                (default) value.
            - All particles have been deactivated (e.g. by stranding)
            - Occurance of any error, whose trace will be output to terminal.

        Before starting a model run, readers must be added for all
        required variables, unless fallback values have been specified.
        Some particles/elements must have been scheduled for seeding, and the
        run will start at the time when the first element has been scheduled..

        Arguments:
            time_step: interval between particles updates, in seconds or as
                timedelta. Default: 3600 seconds (1 hour)
            time_step_output: Time step at which element properties are stored
                and eventually written to file.
                Timedelta object or seconds.
                Default: same as time_step, meaning that all steps are stored
            The length of the simulation is specified by defining one 
                (and only one) of the following parameters:
                - steps: integer, maximum number of steps. End of simulation
                    will be self.start_time + steps*self.time_step
                - duration: timedelta defining the length of the simulation
                - end_time: datetime object defining the end of the simulation
            export_variables: list of variables and parameter names to be 
                saved to file. Default is None (all variables are saved)
        """

        self.timer_end('configuration')
        self.timer_start('preparing main loop')
        # Check that configuration is proper
        validation = self.configobj.validate(validate.Validator())
        if validation is True:
            logging.info('Config validation OK')
        else:
            raise ValueError('Configuration error: ' + str(validation))

        if self.num_elements_scheduled() == 0:
            raise ValueError('Please seed elements before starting a run.')
        self.elements = self.ElementType()

        if outfile is None and export_buffer_length is not None:
            logging.debug('No output file is specified, '
                          'neglecting export_buffer_length')
            export_buffer_length = None

        # Set projection to latlong if not taken from any of the readers
        if self.proj is not None and not (self.proj.is_latlong() or
            'proj=merc' in self.proj.srs): 
            for vector_component in vector_pairs_xy:
                for component in vector_component:
                    if component in self.fallback_values and \
                            self.fallback_values[component] != 0:
                        logging.info('Setting SRS to latlong, since non-zero '
                                     'value used for fallback vectors (%s)' %
                                     component)
                        self.set_projection('+proj=latlong')
        if self.proj is None:
            logging.info('Setting SRS to latlong, since not defined before.')
            self.set_projection('+proj=latlong')

        missing_variables = self.missing_variables()
        missing_variables = [m for m in missing_variables if
                             m != 'land_binary_mask']
        if len(missing_variables) > 0:
            has_fallback = [var for var in missing_variables
                            if var in self.fallback_values]
            if has_fallback == missing_variables:
                logging.info('Fallback values will be used for the following '
                             'variables which have no readers: ')
                for var in has_fallback:
                    logging.info('\t%s: %f' % (var, self.fallback_values[var]))
            else:
                raise ValueError('Readers must be added for the '
                                 'following required variables: ' +
                                 str(missing_variables))

        # Some cleanup needed if starting from imported state
        if self.steps_calculation >= 1:
            self.steps_calculation = 0
        if hasattr(self, 'history'):
            # Delete history matrix before new run
            delattr(self, 'history')
            # Renumbering elements from 0 to num_elements, necessary fix when
            # importing from file, where elements may have been deactivated
            # TODO: should start from 1?
            self.elements.ID = np.arange(0, self.num_elements_active())

        ########################
        # Simulation time step
        ########################
        if type(time_step) is not timedelta:
            # Time step may be given in seconds, as alternative to timedelta
            time_step = timedelta(seconds=time_step)
        self.time_step = time_step
        if time_step_output is None:
            self.time_step_output = self.time_step
        else:
            if type(time_step_output) is timedelta:
                self.time_step_output = time_step_output
            else:
                self.time_step_output = timedelta(seconds=time_step_output)
            if self.time_step_output.days >= 0 and self.time_step.days < 0:
                self.time_step_output = -self.time_step_output

        time_step_ratio = self.time_step_output.total_seconds() / \
            self.time_step.total_seconds()
        if time_step_ratio < 1:
            raise ValueError('Output time step must be equal or larger '
                             'than calculation time step.')
        if not time_step_ratio.is_integer():
            raise ValueError('Ratio of calculation and output time steps '
                             'must be an integer - given ratio is %s' %
                             time_step_ratio)
        ########################
        # Simulation duration
        ########################
        if (duration is not None and end_time is not None) or \
            (duration is not None and steps is not None) or \
                (steps is not None and end_time is not None):
            raise ValueError('Only one of "steps", "duration" and "end_time" '
                             'may be provided simultaneously')
        if duration is None and end_time is None:
            if steps is not None:
                duration = steps*self.time_step
            else:
                for reader in self.readers.values():
                    if reader.end_time is not None:
                        if end_time is None:
                            end_time = reader.end_time
                        else:
                            end_time = min(end_time, reader.end_time)
                    logging.info('Duration, steps or end time not specified, '
                                 'running until end of first reader: %s' %
                                 (end_time))
        if duration is None:
            duration = end_time - self.start_time
        if time_step.days < 0 and duration.days >= 0:
            # Duration shall also be negative for backwards run
            duration = -duration
        self.expected_steps_output = duration.total_seconds() / \
            self.time_step_output.total_seconds() + 1  # Includes start and end
        self.expected_steps_calculation = duration.total_seconds() / \
            self.time_step.total_seconds()
        self.expected_steps_output = int(self.expected_steps_output)
        self.expected_steps_calculation = int(self.expected_steps_calculation)

        ##############################################################
        # If no basemap has been added, we determine it dynamically
        ##############################################################
        # TODO: some more error checking here
        if 'land_binary_mask' in self.required_variables and \
                'land_binary_mask' not in self.priority_list \
                and 'land_binary_mask' not in self.fallback_values:
            logging.info(
                'Adding a dynamical landmask (resolution "%s") based on '
                'assumed maximum speed of %s m/s. '
                'Adding a customised landmask may be faster...' %
                (self.get_config('general:basemap_resolution'), self.max_speed))
            self.timer_start('preparing main loop:making dynamical landmask')
            max_distance = \
                self.max_speed*self.expected_steps_calculation * \
                np.abs(self.time_step.total_seconds())
            deltalat = max_distance/111000.
            deltalon = deltalat/np.cos(
                np.radians(np.mean(self.elements_scheduled.lat)))
            from opendrift.readers import reader_basemap_landmask
            reader_basemap = reader_basemap_landmask.Reader(
                llcrnrlon=self.elements_scheduled.lon.min() - deltalon,
                urcrnrlon=self.elements_scheduled.lon.max() + deltalon,
                llcrnrlat=self.elements_scheduled.lat.min() - deltalat,
                urcrnrlat=np.minimum(89, self.elements_scheduled.lat.max() +
                                     deltalat),
                resolution=self.get_config('general:basemap_resolution'),
                                    projection='merc')
            self.add_reader(reader_basemap)
            self.dynamical_landmask = True
            self.timer_end('preparing main loop:making dynamical landmask')
        else:
            self.dynamical_landmask = False

        ####################################################################
        # Preparing history array for storage in memory and eventually file
        ####################################################################
        if export_buffer_length is None:
            self.export_buffer_length = self.expected_steps_output
        else:
            self.export_buffer_length = export_buffer_length

        if self.time_step.days < 0:
            # For backwards simulation, we start at last seeded element
            logging.info('Backwards simulation, starting at '
                         'time of last seeded element')
            self.time = self.elements_scheduled_time.max()
            # Flipping ID array, so that lowest IDs are released first
            self.elements_scheduled.ID = \
                np.flipud(self.elements_scheduled.ID)
        else:
            # Forward simulation, start time has been set when seeding
            self.time = self.start_time

        # Add the output variables which are always required
        if export_variables is not None:
            export_variables = list(set(export_variables +
                                        ['lon', 'lat', 'ID', 'status']))
        self.export_variables = export_variables
        # Initialise array to hold history (element properties and environment)
        # for export to file.
        history_dtype_fields = [(name,
                                 self.ElementType.variables[name]['dtype'])
                                for name in self.ElementType.variables]
        # Add environment variables
        self.history_metadata = self.ElementType.variables.copy()
        for env_var in self.required_variables:
            history_dtype_fields.append((env_var, np.dtype('float32')))
            self.history_metadata[env_var] = {}

        # Remove variables from output array, if only subset is requested
        if self.export_variables is not None:
            history_dtype_fields = [f for f in history_dtype_fields
                                    if f[0] in self.export_variables]
            for m in self.history_metadata:
                if m not in self.export_variables:
                    del self.history_metadata[m]

        history_dtype = np.dtype(history_dtype_fields)
        self.history = np.ma.array(np.zeros((len(self.elements_scheduled),
                                             self.export_buffer_length)),
                                   dtype=history_dtype)
        self.history.mask = True
        self.steps_exported = 0

        if outfile is not None:
            self.io_init(outfile, times=self.expected_steps_output)
        else:
            self.outfile = None

        #############################
        # Model specific preparation
        #############################
        self.prepare_run()

        ##########################
        # Main loop
        ##########################
        self.timer_end('preparing main loop')
        self.timer_start('main loop')
        for i in range(self.expected_steps_calculation):
            try:
                # Release elements
                self.release_elements()

                self.lift_elements_to_seafloor()  # If seafloor is penetrated

                # Display time to terminal
                logging.debug('==================================='*2)
                logging.info('%s - step %i of %i - %i active elements '
                             '(%i deactivated)' %
                             (self.time, self.steps_calculation + 1,
                              self.expected_steps_calculation,
                              self.num_elements_active(),
                              self.num_elements_deactivated()))
                logging.debug('%s elements scheduled.' %
                              self.num_elements_scheduled())
                logging.debug('==================================='*2)

                self.environment, self.environment_profiles, missing = \
                    self.get_environment(self.required_variables,
                                         self.time,
                                         self.elements.lon,
                                         self.elements.lat,
                                         self.elements.z,
                                         self.required_profiles)

                self.interact_with_coastline()

                self.deactivate_elements(missing, reason='missing_data')

                self.state_to_buffer()  # Append status to history array

                self.remove_deactivated_elements()

                # Propagate one timestep forwards
                self.steps_calculation += 1

                if self.num_elements_active() == 0:
                    raise ValueError('No more active elements, quitting.')

                # Store location, in case elements shall be moved back
                self.store_present_positions()

                #####################################################
                logging.debug('Calling %s.update()' %
                              type(self).__name__)
                self.timer_start('main loop:updating elements')
                self.update()
                self.timer_end('main loop:updating elements')
                #####################################################

                #self.lift_elements_to_seafloor()  # If seafloor is penetrated

                if self.num_elements_active() == 0:
                    raise ValueError('No active elements, quitting simulation')

                logging.debug('%s active elements (%s deactivated)' %
                              (self.num_elements_active(),
                               self.num_elements_deactivated()))
                # Updating time
                if self.time is not None:
                    self.time = self.time + self.time_step

            except Exception as e:
                logging.info('========================')
                logging.info('End of simulation:')
                logging.info(e)
                logging.info(traceback.format_exc())
                logging.info('========================')
                if stop_on_error is True:
                    import sys; sys.exit('Stopping on error')
                break

        self.timer_end('main loop')
        self.timer_start('cleaning up')
        logging.debug('Cleaning up')

        self.interact_with_coastline()
        self.state_to_buffer()  # Append final status to buffer

        if outfile is not None:
            logging.debug('Writing and closing output file: %s' % outfile)
            # Write buffer to outfile, and close
            if self.steps_output >= self.steps_exported:
                # Write last lines, if needed
                self.io_write_buffer()
            self.io_close()

        # Remove any elements scheduled for deactivation during last step
        #self.remove_deactivated_elements()

        if export_buffer_length is None:
            # Remove columns for unseeded elements in history array
            self.history = self.history[
                range(self.num_elements_activated()), :]
            # Remove rows for unreached timsteps in history array
            self.history = self.history[:, range(self.steps_output)]
        else:  # If output has been flushed to file during run, we
               # need to reimport from file to get all data in memory
            del self.environment
            if hasattr(self, 'environment_profiles'):
                del self.environment_profiles
            self.io_import_file(outfile)

        if self.dynamical_landmask is True:
            self.zoom_map(buffer=.2)  # Zooming to extent of trajectories

        self.timer_end('cleaning up')
        self.timer_end('total time')

    def state_to_buffer(self):
        """Append present state (elements and environment) to recarray."""

        steps_calculation_float = \
            (self.steps_calculation * self.time_step.total_seconds() /
             self.time_step_output.total_seconds()) + 1
        self.steps_output = int(np.floor(steps_calculation_float))

        ID_ind = self.elements.ID - 1
        time_ind = self.steps_output - 1 - self.steps_exported

        if steps_calculation_float.is_integer():
            element_ind = range(len(ID_ind))  # We write all elements
        else:
            deactivated = np.where(self.elements.status != 0)[0]
            if len(deactivated) == 0:
                    return  # No deactivated elements this sub-timestep
            # We write history for deactivated elements only:
            logging.debug('Writing history for %s deactivated elements' %
                          len(deactivated))
            ID_ind = ID_ind[deactivated]
            element_ind = deactivated
            time_ind = np.minimum(time_ind + 1, self.history.shape[1] - 1)

        # Store present state in history recarray
        for i, var in enumerate(self.elements.variables):
            if self.export_variables is not None and \
                    var not in self.export_variables:
                continue
            # Temporarily assuming elements numbered
            # from 0 to num_elements_active()
            # Does not hold when importing ID from a saved file, where
            # some elements have been deactivated
            self.history[var][ID_ind, time_ind] = \
                getattr(self.elements, var)[element_ind]
        # Copy environment data to history array
        for i, var in enumerate(self.environment.dtype.names):
            if self.export_variables is not None and \
                    var not in self.export_variables:
                continue
            self.history[var][ID_ind, time_ind] = \
                getattr(self.environment, var)[element_ind]

        # Call writer if buffer is full
        if (self.outfile is not None) and \
                ((self.steps_output - self.steps_exported) ==
                    self.export_buffer_length):
            self.io_write_buffer()

    def index_of_activation_and_deactivation(self):
        """Return the indices when elements were seeded and deactivated."""

        firstlast = np.ma.notmasked_edges(self.history['lon'], axis=1)
        index_of_activation = firstlast[0][1]
        index_of_deactivation = firstlast[1][1]
        return index_of_activation, index_of_deactivation

    def set_up_map(self, buffer=.1, delta_lat=None, **kwargs):
        """Generate Basemap instance on which trajectories are plotted."""

        lons, lats = self.get_lonlats()

        # Initialise map
        lonmin = lons.min() - buffer*2
        lonmax = lons.max() + buffer*2
        latmin = lats.min() - buffer
        latmax = lats.max() + buffer
        if 'basemap_landmask' in self.readers:
            # Using an eventual Basemap already used to check stranding
            map = self.readers['basemap_landmask'].map
        else:
            # Otherwise create a new Basemap covering the elements
            ## Calculate aspect ratio, to minimise whitespace on figures
            ## Drawback is that empty figure is created in interactive mode
            meanlat = (latmin + latmax)/2
            aspect_ratio = \
                np.float(latmax-latmin) / (np.float(lonmax-lonmin))
            aspect_ratio = aspect_ratio / np.cos(np.radians(meanlat))
            if aspect_ratio > 1:
                plt.figure(figsize=(10./aspect_ratio, 10.))
            else:
                plt.figure(figsize=(11., 11.*aspect_ratio))
            #ax = plt.axes([.05,.05,.85,.9])
            ax = plt.axes([.05, .08, .85, .9])  # When colorbar below
            map = Basemap(lonmin, latmin, lonmax, latmax,
                          resolution=
                            self.get_config('general:basemap_resolution'),
                          projection='merc')

        map.drawcoastlines(color='gray')
        map.fillcontinents(color='#ddaa99')
        if delta_lat is None:
            # Adjusting spacing of lon-lat lines dynamically
            latspan = map.latmax - map.latmin
            if latspan > 20:
                delta_lat = 2
            elif latspan > 10 and latspan <= 20:
                delta_lat = 1
            elif latspan > 1 and latspan <= 10:
                delta_lat = .5
            else:
                delta_lat = .1
            lonspan = map.lonmax - map.lonmin
            if lonspan > 20:
                delta_lon = 4
            elif lonspan > 10 and lonspan <= 20:
                delta_lon = 2
            elif lonspan > 1 and lonspan <= 10:
                delta_lon = 1
            else:
                delta_lon = .2
        if delta_lat != 0:
            map.drawmeridians(np.arange(np.floor(map.lonmin),
                                        np.ceil(map.lonmax), delta_lon),
                              labels=[0, 0, 0, 1])
            try:
                map.drawparallels(np.arange(np.floor(map.latmin),
                                            np.ceil(map.latmax), delta_lat),
                                  labels=[0, 1, 1, 0])
            except:
                logging.info('Drawing of parallels failed due to bug in '
                             'matplotlib, can be fixed as explained here: '
                'https://sourceforge.net/p/matplotlib/mailman/message/28461289/')
                map.drawparallels(np.arange(np.floor(map.latmin),
                                            np.ceil(map.latmax), 1),
                                  labels=[0, 1, 1, 0])
        x, y = map(lons, lats)

        try:
            firstlast = np.ma.notmasked_edges(x, axis=1)
            index_of_first = firstlast[0][1]
            index_of_last = firstlast[1][1]
        except:
            index_of_last = 0

        try:  # Activate figure zooming
            mng = plt.get_current_fig_manager()
            mng.toolbar.zoom()
        except:
            pass

        try:  # Maximise figure window size
            mng.resize(*mng.window.maxsize())
        except:
            pass

        return map, plt, x, y, index_of_first, index_of_last

    def zoom_map(self, buffer=0.2,
                 lonmin=None, lonmax=None, latmin=None, latmax=None):
        """Zoom Basemap to defined limits, or defined buffer in degrees"""

        if lonmin is None:
            lons, lats = self.get_lonlats()
            lonmin = lons.min() - buffer*2
            lonmax = lons.max() + buffer*2
            latmin = lats.min() - buffer
            latmax = lats.max() + buffer

        logging.info('Zooming basemap to (%s to %s E), (%s to %s N)' %
                     (lonmin, lonmax, latmin, latmax) )
        if 'basemap_landmask' in self.readers:
            self.readers['basemap_landmask'].map_orig = \
                self.readers['basemap_landmask'].map
        else:
            raise ValueError('No basemap readers added.')
        map = Basemap(lonmin, latmin, lonmax, latmax,
                      resolution=None, projection='merc')
        map_orig = self.readers['basemap_landmask'].map_orig  # pointer

        # Find Basemap offset between new and old maps
        xo, yo = map(lonmin, latmin, inverse=False)
        xoo, yoo = map_orig(lonmin, latmin, inverse=False)
        xoff = xoo-xo
        yoff = yoo-yo

        # Copy polygons and adjust for offsets
        map.coastpolygons = [
            (tuple(np.subtract(pol[0], xoff)),
            tuple(np.subtract(pol[1], yoff))) for pol in
            map_orig.coastpolygons]

        coastsegs_new = []
        for c in map_orig.coastsegs:
            xc, yc = zip(*c)
            newxc = tuple(xce - xoff for xce in xc)
            newyc = tuple(yce - yoff for yce in yc)
            coastsegs_new.append(zip(newxc, newyc))
        map.coastsegs = coastsegs_new

        map.coastpolygontypes = map_orig.coastpolygontypes
        #map.landpolygons = map_orig.landpolygons
        #map.lakepolygons = map_orig.lakepolygons
        #map.boundarylons = map_orig.boundarylons
        map.resolution = map_orig.resolution

        self.readers['basemap_landmask'].map = map

    def get_lonlats(self):
        if hasattr(self, 'history'):
            lons = self.history['lon']
            lats = self.history['lat']
        else:
            if self.steps_output > 0:
                lons = np.ma.array(np.reshape(self.elements.lon, (1, -1))).T
                lats = np.ma.array(np.reshape(self.elements.lat, (1, -1))).T
            else:
                lons = np.ma.array(
                    np.reshape(self.elements_scheduled.lon, (1, -1))).T
                lats = np.ma.array(
                    np.reshape(self.elements_scheduled.lat, (1, -1))).T
        return lons, lats

    def animation(self, buffer=.2, filename=None, compare=None,
                  background=None, vmin=None, vmax=None, skip=5, scale=10,
                  legend=['', ''], markersize=5, fps=10):
        """Animate last run."""

        def plot_timestep(i):
            """Sub function needed for matplotlib animation."""
            #plt.gcf().gca().set_title(str(i))
            ax.set_title(times[i])
            if background is not None:
                map_x, map_y, scalar, u_component, v_component = \
                    self.get_map_background(map, background,
                                            time=times[i])
                map.pcolormesh(map_x, map_y, scalar, alpha=1,
                               vmin=vmin, vmax=vmax)
                if type(background) is list:
                    map.quiver(map_x[::skip, ::skip],
                               map_y[::skip, ::skip],
                               u_component[::skip, ::skip],
                               v_component[::skip, ::skip], scale=scale)
            points.set_data(x[range(x.shape[0]), i],
                            y[range(x.shape[0]), i])
            points_deactivated.set_data(
                x_deactive[index_of_last_deactivated < i],
                y_deactive[index_of_last_deactivated < i])

            if compare is not None:
                points_other.set_data(x_other[range(x_other.shape[0]), i],
                                      y_other[range(x_other.shape[0]), i])
                points_other_deactivated.set_data(
                    x_other_deactive[index_of_last_deactivated_other < i],
                    y_other_deactive[index_of_last_deactivated_other < i])
                return points, points_other
            else:
                return points

        # Find map coordinates and plot points with empty data, to be updated
        map, plt, x, y, index_of_first, index_of_last = \
            self.set_up_map(buffer=buffer)
        ax = plt.gcf().gca()

        if background is not None:
            map_x, map_y, scalar, u_component, v_component = \
                self.get_map_background(map, background,
                                        time=self.start_time)
            map.pcolormesh(map_x, map_y, scalar, alpha=1,
                           vmin=vmin, vmax=vmax)

        times = self.get_time_array()[0]
        index_of_last_deactivated = \
            index_of_last[self.elements_deactivated.ID-1]
        points = map.plot([], [], '.k', label=legend[0],
                          markersize=markersize)[0]
        # Plot deactivated elements, with transparency
        points_deactivated = map.plot([], [], '.k', alpha=.3)[0]
        x_deactive, y_deactive = map(self.elements_deactivated.lon,
                                     self.elements_deactivated.lat)

        if compare is not None:
            if type(compare) is str:
                # Other is given as filename
                other = self.__class__(loglevel=0)
                other.io_import_file(compare)
            else:
                # Other is given as an OpenDrift object
                other = compare
            # Find map coordinates and plot data for comparison
            x_other, y_other = map(other.history['lon'], other.history['lat'])
            points_other = map.plot(x_other[0, 0], y_other[0, 0], '.r',
                                    label=legend[1],
                                    markersize=markersize)[0]
            x_other_deactive, y_other_deactive = \
                map(other.elements_deactivated.lon,
                    other.elements_deactivated.lat)
            # Plot deactivated elements, with transparency
            points_other_deactivated = map.plot([], [], '.r', alpha=.3)[0]
            firstlast = np.ma.notmasked_edges(x_other, axis=1)
            index_of_last_other = firstlast[1][1]
            index_of_last_deactivated_other = \
                index_of_last_other[other.elements_deactivated.ID-1]

        if legend != ['', '']:
            plt.legend()

        anim = animation.FuncAnimation(plt.gcf(), plot_timestep, blit=False,
                                       frames=x.shape[1], interval=50)

        if filename is not None:
            self._save_animation(anim, filename, fps)
        else:
            plt.show()

    def animation_profile(self, filename=None, compare=None,
                          legend=['', ''], markersize=5, fps=20):
        """Animate vertical profile of the last run."""

        def plot_timestep(i):
            """Sub function needed for matplotlib animation."""
            #plt.gcf().gca().set_title(str(i))
            ax.set_title(times[i])
            points.set_data(x[range(x.shape[0]), i],
                            z[range(x.shape[0]), i])
            points_deactivated.set_data(
                x_deactive[index_of_last_deactivated < i],
                z_deactive[index_of_last_deactivated < i])

            if compare is not None:
                points_other.set_data(x_other[range(x_other.shape[0]), i],
                                      z_other[range(x_other.shape[0]), i])
                points_other_deactivated.set_data(
                    x_other_deactive[index_of_last_deactivated_other < i],
                    z_other_deactive[index_of_last_deactivated_other < i])
                return points, points_other
            else:
                return points

        # Set up plot
        index_of_first, index_of_last = \
            self.index_of_activation_and_deactivation()
        z = self.get_property('z')[0].T
        x = self.get_property('lon')[0].T
        #seafloor_depth = \
        #    -self.get_property('sea_floor_depth_below_sea_level')[0].T
        fig = plt.figure(figsize=(10, 6.))  # Suitable aspect ratio
        ax = fig.gca()
        plt.xlabel('Longitude [degrees]')
        plt.ylabel('Depth [m]')
        times = self.get_time_array()[0]
        index_of_last_deactivated = \
            index_of_last[self.elements_deactivated.ID-1]
        points = plt.plot([], [], '.k', label=legend[0],
                          markersize=markersize)[0]
        # Plot deactivated elements, with transparency
        points_deactivated = plt.plot([], [], '.k', alpha=.3)[0]
        x_deactive = self.elements_deactivated.lon
        z_deactive = self.elements_deactivated.z

        if compare is not None:
            if type(compare) is str:
                # Other is given as filename
                other = self.__class__(loglevel=0)
                other.io_import_file(compare)
            else:
                # Other is given as an OpenDrift object
                other = compare
            z_other = other.get_property('z')[0].T
            x_other = self.get_property('lon')[0].T
            points_other = plt.plot(x_other[0, 0], z_other[0, 0], '.r',
                                    label=legend[1],
                                    markersize=markersize)[0]
            # Plot deactivated elements, with transparency
            points_other_deactivated = plt.plot([], [], '.r', alpha=.3)[0]
            x_other_deactive = other.elements_deactivated.lon
            z_other_deactive = other.elements_deactivated.z
            firstlast = np.ma.notmasked_edges(x_other, axis=1)
            index_of_last_other = firstlast[1][1]
            index_of_last_deactivated_other = \
                index_of_last_other[other.elements_deactivated.ID-1]
            xmax = np.maximum(x.max(), x_other.max())
            xmin = np.minimum(x.min(), x_other.min())
            zmax = np.maximum(z.max(), z_other.max())
            zmin = np.minimum(z.min(), z_other.min())
        else:
            xmin = x.min()
            xmax = x.max()
            zmin = z.min()
            zmax = z.max()

        # Set figure limits
        sky = (zmax-zmin)*.1  # Sky height is 10% of water depth
        plt.xlim([xmin, xmax])
        plt.ylim([zmin, sky])
        ax.add_patch(plt.Rectangle((xmin, 0), xmax-xmin, sky,
                     color='lightsteelblue'))
        ax.add_patch(plt.Rectangle((xmin, zmin), xmax-xmin,
                     -zmin, color='cornflowerblue'))

        if legend != ['', '']:
            plt.legend(loc=4)

        anim = animation.FuncAnimation(plt.gcf(), plot_timestep, blit=False,
                                       frames=x.shape[1], interval=150)

        if filename is not None:
            self._save_animation(anim, filename, fps)
        else:
            plt.show()

    def plot(self, background=None, buffer=.2, linecolor=None, filename=None,
             drifter_file=None, show=True, vmin=None, vmax=None,
             lvmin=None, lvmax=None, skip=2, scale=10, show_scalar=True,
             contourlines=False, trajectory_dict=None, **kwargs):
        """Basic built-in plotting function intended for developing/debugging.

        Plots trajectories of all particles.
        Positions marked with colored stars:
        - green: all start positions
        - red: deactivated particles
        - blue: particles still active at end of simulation

        Requires availability of Basemap.

        Arguments:
            background: string, name of variable (standard_name) which will
                be plotted as background of trajectories, provided that it
                can be read with one of the available readers.
            buffer: float; spatial buffer of plot in degrees of
                longitude/latitude around particle collection.
            background: name of variable to be plotted as background field.
                        Use two element list for vector fields, e.g.
                        ['x_wind', 'y_wind']
            vmin, vmax: minimum and maximum values for colors of background.
            linecolor: name of variable to be used for coloring trajectories.
            lvmin, lvmax: minimum and maximum values for colors of trajectories.
        """

        map, plt, x, y, index_of_first, index_of_last = \
            self.set_up_map(buffer=buffer, **kwargs)

        # The more elements, the more transparent we make the lines
        min_alpha = 0.1
        max_elements = 5000.0
        alpha = min_alpha**(2*(self.num_elements_total()-1)/(max_elements-1))
        alpha = np.max((min_alpha, alpha))
        if hasattr(self, 'history'):
            # Plot trajectories
            if linecolor is None:
                map.plot(x.T, y.T, color='gray', alpha=alpha)
            else:
                # Color lines according to given parameter
                try:
                    param = self.history[linecolor]
                except:
                    raise ValueError(
                        'Available parameters to be used for linecolors: ' +
                        str(self.history.dtype.fields))
                from matplotlib.collections import LineCollection
                for i in range(x.shape[0]):
                    vind = np.arange(index_of_first[i], index_of_last[i] + 1)
                    points = np.array(
                        [x[i, vind].T, y[i, vind].T]).T.reshape(-1, 1, 2)
                    segments = np.concatenate([points[:-1], points[1:]],
                                              axis=1)
                    if lvmin is None:
                        lvmin = param.min()
                        lvmax = param.max()
                    lc = LineCollection(segments,
                                        cmap=plt.get_cmap('Spectral'),
                                        norm=plt.Normalize(lvmin, lvmax))
                    #lc.set_linewidth(3)
                    lc.set_array(param.T[vind, i])
                    plt.gca().add_collection(lc)
                #axcb = map.colorbar(lc, location='bottom', pad='5%')
                axcb = map.colorbar(lc, location='bottom', pad='1%')
                try:  # Add unit to colorbar if available
                    colorbarstring = linecolor + '  [%s]' % \
                        (self.history_metadata[linecolor]['units'])
                except:
                    colorbarstring = linecolor
                #axcb.set_label(colorbarstring)
                axcb.set_label(colorbarstring, size=14)
                axcb.ax.tick_params(labelsize=14)

        map.scatter(x[range(x.shape[0]), index_of_first],
                    y[range(x.shape[0]), index_of_first],
                    zorder=10, edgecolor='k', linewidths=.2,
                    color=self.status_colors['initial'],
                    label='initial (%i)' % x.shape[0])
        map.scatter(x[range(x.shape[0]), index_of_last],
                    y[range(x.shape[0]), index_of_last],
                    zorder=3, edgecolor='k', linewidths=.2,
                    color=self.status_colors['active'],
                    label='active (%i)' %
                    (x.shape[0] - self.num_elements_deactivated()))

        x_deactivated, y_deactivated = map(self.elements_deactivated.lon,
                                           self.elements_deactivated.lat)
        # Plot deactivated elements, labeled by deactivation reason
        for statusnum, status in enumerate(self.status_categories):
            if status == 'active':
                continue  # plotted above
            if status not in self.status_colors:
                # If no color specified, pick an unused one
                for color in ['red', 'blue', 'green', 'black', 'gray',
                              'cyan', 'DarkSeaGreen', 'brown']:
                    if color not in self.status_colors.values():
                        self.status_colors[status] = color
                        break
            indices = np.where(self.elements_deactivated.status == statusnum)
            if len(indices[0]) > 0:
                if (status == 'seeded_on_land' or
                    status == 'seeded_at_nodata_position'):
                    zorder = 11
                else:
                    zorder = 3
                map.scatter(x_deactivated[indices], y_deactivated[indices],
                            zorder=zorder, edgecolor='k', linewidths=.1,
                            color=self.status_colors[status],
                            label='%s (%i)' % (status, len(indices[0])))
        try:
            plt.legend(loc='best')
            #plt.legend(loc='lower right')
        except Exception as e:
            print 'Cannot plot legend, due to bug in matplotlib:'
            print traceback.format_exc()

        if background is not None:
            map_x, map_y, scalar, u_component, v_component = \
                self.get_map_background(map, background, time=self.time -
                                        self.time_step_output)

            if show_scalar is True:
                if contourlines is False:
                    map.pcolormesh(map_x, map_y, scalar, alpha=1,
                                   vmin=vmin, vmax=vmax)
                else:
                    if contourlines is True:
                        CS = map.contour(map_x, map_y, scalar,
                                         colors='gray')
                    else:
                        # contourlines is an array of values
                        CS = map.contour(map_x, map_y, scalar, contourlines,
                                         colors='gray')
                    plt.clabel(CS, fmt='%g')

            if type(background) is list:
                map.quiver(map_x[::skip, ::skip], map_y[::skip, ::skip],
                           u_component[::skip, ::skip],
                           v_component[::skip, ::skip], scale=scale)

        if hasattr(self, 'time'):
            plt.title(type(self).__name__ + '  %s to %s (%i steps)' %
                      (self.start_time.strftime('%Y-%m-%d %H:%M'),
                       self.time.strftime('%Y-%m-%d %H:%M'),
                       self.steps_output))
        else:
            plt.title(type(self).__name__ + ' - %i elements seeded at %s' %
                      (self.num_elements_scheduled(),
                       self.elements_scheduled_time[0].strftime(
                       '%Y-%m-%d %H:%M')))

        if drifter_file is not None:
            # Format of joubeh.com
            for dfile in drifter_file:
                data = np.recfromcsv(dfile)
                x, y = map(data['longitude'], data['latitude'])
                map.plot(x, y, '-k', linewidth=2)
                map.plot(x[0], y[0], '*k')
                map.plot(x[-1], y[-1], '*k')

            # Format for shell buoy
            #data = np.loadtxt(drifter_file, skiprows=1, usecols=(2, 3))
            #x, y = map(data.T[1], data.T[0])
            #map.plot(x, y, '-r', linewidth=2, zorder=10)
            #map.plot(x[0], y[0], '*r', zorder=10)
            #map.plot(x[-1], y[-1], '*r', zorder=10)

        if trajectory_dict is not None:
            self._plot_trajectory_dict(map, trajectory_dict)

        #plt.gca().tick_params(labelsize=14)

        if filename is not None:
            #plt.savefig(filename, dpi=200)
            plt.savefig(filename)
            plt.close()
        else:
            if show is True:
                plt.show()

        return map, plt

    def _plot_trajectory_dict(self, map, trajectory_dict):
        '''Plot provided trajectory along with simulated'''
        time = trajectory_dict['time']
        time = np.array(time)
        i = np.where((time>=self.start_time) & (time<=self.time))[0]
        x, y = map(trajectory_dict['lon'][i], trajectory_dict['lat'][i])
        ls = trajectory_dict['linestyle']
        map.plot(x, y, ls, linewidth=2)
        map.plot(x[0], y[0], 'ok')
        map.plot(x[-1], y[-1], 'xk')

    def get_map_background(self, map, background, time=None):
        # Get background field for plotting on map or animation
        if time is None:
            time = self.time - self.time_step_output
        if type(background) is list:
            variable = background[0]  # A vector is requested
        else:
            variable = background  # A scalar is requested
        for readerName in self.readers:
            reader = self.readers[readerName]
            if variable in reader.variables:
                break
        # Get lat/lons of ny by nx evenly space grid.
        lons, lats = map.makegrid(4, 4)
        reader_x, reader_y = reader.lonlat2xy(lons, lats)
        data = reader.get_variables(
            background, time, reader_x, reader_y, 0, block=True)
        reader_x, reader_y = np.meshgrid(data['x'], data['y'])
        if type(background) is list:
            u_component = data[background[0]]
            v_component = data[background[1]]
            scalar = np.sqrt(u_component**2 + v_component**2)
            # NB: rotation not completed!
            u_component, v_component = reader.rotate_vectors(
                reader_x, reader_y, u_component, v_component,
                reader.proj, map.srs)
        else:
            scalar = data[background]
            u_component = v_component = None
        rlons, rlats = reader.xy2lonlat(reader_x, reader_y)
        map_x, map_y = map(rlons, rlats)

        return map_x, map_y, scalar, u_component, v_component

    def get_density_array(self, pixelsize_m):
        lon = self.get_property('lon')[0]
        lat = self.get_property('lat')[0]
        z = self.get_property('z')[0]
        status = self.get_property('status')[0]
        lon_submerged = lon.copy()
        lat_submerged = lat.copy()
        lon_submerged[z>=0] = 1000
        lat_submerged[z>=0] = 1000
        lon_stranded = lon.copy()
        lat_stranded = lat.copy()
        times = self.get_time_array()[0]
        deltalat = pixelsize_m/111000.0  # m to degrees
        deltalon = deltalat/np.cos(np.radians((lat.min() +
                                               lat.max())/2))
        lat_array = np.arange(lat.min()-deltalat,
                              lat.max()+deltalat, deltalat)
        lon_array = np.arange(lon.min()-deltalat,
                              lon.max()+deltalon, deltalon)
        H = np.zeros((len(times), len(lon_array) - 1,
                      len(lat_array) - 1)).astype(int)
        H_submerged = H.copy()
        H_stranded = H.copy()
        try:
            strandnum = self.status_categories.index('stranded')
            lon_stranded[status!=strandnum] = 1000
            lat_stranded[status!=strandnum] = 1000
            contains_stranded = True
        except ValueError:
            contains_stranded = False
        lon[z<0] = 1000
        lat[z<0] = 1000
        for i, t in enumerate(times):
            H[i,:,:], lon_array, lat_array = \
                np.histogram2d(lon[i,:],
                               lat[i,:], bins=(lon_array, lat_array))
            H_submerged[i,:,:], dummy, dummy = \
                np.histogram2d(lon_submerged[i,:],
                               lat_submerged[i,:], bins=(lon_array, lat_array))
            if contains_stranded is True:
                H_stranded[i,:,:], dummy, dummy = \
                np.histogram2d(lon_stranded[i,:],
                               lat_stranded[i,:],
                               bins=(lon_array, lat_array))


        return H, H_submerged, H_stranded, lon_array, lat_array

    def write_netcdf_density_map(self, filename, pixelsize_m=300):
        '''Write netCDF file with map of particles densities'''
        H, H_submerged, H_stranded, lon_array, lat_array = \
            self.get_density_array(pixelsize_m)
        lon_array = (lon_array[0:-1] + lon_array[1::])/2
        lat_array = (lat_array[0:-1] + lat_array[1::])/2

        from netCDF4 import Dataset, date2num
        nc = Dataset(filename, 'w')
        nc.createDimension('lon', len(lon_array))
        nc.createDimension('lat', len(lat_array))
        nc.createDimension('time', H.shape[0])
        times = self.get_time_array()[0]
        timestr = 'seconds since 1970-01-01 00:00:00'
        nc.createVariable('time', 'f8', ('time',))
        nc.variables['time'][:] = date2num(times, timestr)
        nc.variables['time'].units = timestr
        nc.variables['time'].standard_name = 'time'
        # Projection
        nc.createVariable('projection_lonlat', 'i8')
        nc.variables['projection_lonlat'].grid_mapping_name = \
            'latitude_longitude'
        nc.variables['projection_lonlat'].earth_radius = 6371229.
        nc.variables['projection_lonlat'].proj4 = \
            '+proj=longlat +a=6371229 +no_defs'
        # Coordinates
        nc.createVariable('lon', 'f8', ('lon',))
        nc.createVariable('lat', 'f8', ('lat',))
        nc.variables['lon'][:] = lon_array
        nc.variables['lon'].long_name = 'longitude'
        nc.variables['lon'].short_name = 'longitude'
        nc.variables['lon'].units = 'degrees_east'
        nc.variables['lat'][:] = lat_array
        nc.variables['lat'].long_name = 'latitude'
        nc.variables['lat'].short_name = 'latitude'
        nc.variables['lat'].units = 'degrees_north'
        # Density
        nc.createVariable('density_surface', 'u1',
                          ('time','lat', 'lon'))
        H = np.swapaxes(H, 1, 2).astype('uint8')
        H = np.ma.masked_where(H==0, H)
        nc.variables['density_surface'][:] = H
        nc.variables['density_surface'].long_name = 'Detection probability'
        nc.variables['density_surface'].grid_mapping = 'projection_lonlat'
        nc.variables['density_surface'].units = '1'
        # Density submerged
       	nc.createVariable('density_submerged', 'u1',
                          ('time','lat', 'lon'))
        H_sub = np.swapaxes(H_submerged, 1, 2).astype('uint8')
        H_sub = np.ma.masked_where(H_sub==0, H_sub)
        nc.variables['density_submerged'][:] = H_sub
        nc.variables['density_submerged'].long_name = 'Detection probability submerged'
        nc.variables['density_submerged'].grid_mapping = 'projection_lonlat'
        nc.variables['density_submerged'].units = '1' 
        # Density stranded
       	nc.createVariable('density_stranded', 'u1',
                          ('time','lat', 'lon'))
        H_stranded = np.swapaxes(H_stranded, 1, 2).astype('uint8')
        H_stranded = np.ma.masked_where(H_stranded==0, H_stranded)
        nc.variables['density_stranded'][:] = H_stranded
        nc.variables['density_stranded'].long_name = 'Detection probability stranded'
        nc.variables['density_stranded'].grid_mapping = 'projection_lonlat'
        nc.variables['density_stranded'].units = '1' 
 
        nc.close()

    def write_geotiff(self, filename, pixelsize_km=.2):
        '''Write one GeoTiff image per timestep.

        filename should contain date identifiers, e.g. 'img_%Y%m%d_%H%M.tif'
        https://docs.python.org/2/library/datetime.html#strftime-and-strptime-behavior
        '''

        try:
            import gdal
            import osr
        except:
            raise ValueError('GDAL is needed to write geotiff images.')
        import matplotlib.pyplot as plt
        driver = gdal.GetDriverByName('GTiff')
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        colortable = gdal.ColorTable()
        colortable.SetColorEntry(0,(255,255,255, 0))
        colortable.SetColorEntry(1,(0,0,0, 255))
        colortable.SetColorEntry(2,(255,0,0, 255))
        colortable.SetColorEntry(3,(0,255,0, 255))
        colortable.SetColorEntry(4,(0,0,255, 255))

        lon = self.get_property('lon')[0]
        lat = self.get_property('lat')[0]
        status = self.get_property('status')[0]
        times = self.get_time_array()[0]
        deltalat = pixelsize_km/111.0  # km to degrees
        deltalon = deltalat/np.cos(np.radians((lat.min() + lat.max())/2))
        lat_array = np.arange(lat.min()-deltalat, lat.max()+deltalat, deltalat)
        lon_array = np.arange(lon.min()-deltalat, lon.max()+deltalon, deltalon)
        ilon = (np.round((lon-lon.min())/deltalon)).astype(int)
        ilat = (np.round((lat-lat.min())/deltalat)).astype(int)
        # Setting masked values to zero, for use as indices
        ilon[ilon.mask] = 0
        ilat[ilat.mask] = 0
        status[ilon.mask] = 0
        image = np.zeros((len(times), len(lon_array),
                          len(lat_array))).astype(int)
        geotransform = [lon_array.min(), deltalon, 0,
                        lat_array.max(), 0, -deltalat]
        for i, t in enumerate(times):
            image[i, ilon[i,:], ilat[i,:]] = status[i, :] + 1
            filename_i = t.strftime(filename)
            ds = driver.Create(filename_i, len(lon_array), len(lat_array),
                               1, gdal.GDT_Byte, )
            ds.SetProjection(srs.ExportToWkt())
            ds.SetGeoTransform(geotransform)
            outband=ds.GetRasterBand(1)
            outband.SetNoDataValue(0)
            outband.WriteArray(np.fliplr(image[i, :, :]).transpose())
            outband.SetColorTable(colortable)
            ds = None

    def get_time_array(self):
        """Return a list of output times of last run."""
        td = self.time_step_output
        time_array = [self.start_time + td*i for i in
                      range(self.steps_output)]
        time_array_relative = [td*i for i in range(self.steps_output)]
        return time_array, time_array_relative

    def plot_environment(self):
        """Plot mean wind and current velocities of element of last run."""
        x_wind = self.get_property('x_wind')[0]
        y_wind = self.get_property('x_wind')[0]
        wind = np.sqrt(x_wind**2 + y_wind**2)
        x_sea_water_velocity = self.get_property('x_sea_water_velocity')[0]
        y_sea_water_velocity = self.get_property('y_sea_water_velocity')[0]
        current = np.sqrt(x_sea_water_velocity**2 + y_sea_water_velocity**2)
        wind = np.ma.mean(wind, axis=1)
        current = np.ma.mean(current, axis=1)
        time, time_relative = self.get_time_array()
        time = np.array([t.total_seconds()/3600. for t in time_relative])

        fig = plt.figure()
        ax1 = fig.add_subplot(111)
        ax1.plot(time, wind, 'b', label='wind speed')
        ax1.set_ylabel('Wind speed  [m/s]', color='b')
        ax1.set_xlim([0, time[-1]])
        ax1.set_ylim([0, wind.max()])

        ax2 = ax1.twinx()
        ax2.plot(time, current, 'r', label='current speed')
        ax2.set_ylabel('Current speed  [m/s]', color='r')
        ax2.set_xlim([0, time[-1]])
        ax2.set_ylim([0, current.max()])
        for tl in ax1.get_yticklabels():
            tl.set_color('b')
        for tl in ax2.get_yticklabels():
            tl.set_color('r')
        ax1.set_xlabel('Time  [hours]')
        plt.show()

    def plot_property(self, prop, mean=False):
        """Basic function to plot time series of any element properties."""
        import matplotlib.pyplot as plt
        from matplotlib import dates

        hfmt = dates.DateFormatter('%d %b %Y %H:%M')
        fig = plt.figure()
        ax = fig.gca()
        ax.xaxis.set_major_formatter(hfmt)
        plt.xticks(rotation='vertical')
        times = [self.start_time + n*self.time_step_output
                 for n in range(self.steps_output)]
        data = self.history[prop].T[0:len(times), :]
        if mean is True:  # Taking average over elements
            data = np.mean(data, axis=1)
        plt.plot(times, data)
        plt.title(prop)
        plt.xlabel('Time  [UTC]')
        try:
            plt.ylabel('%s  [%s]' %
                       (prop, self.elements.variables[prop]['units']))
        except:
            plt.ylabel(prop)
        plt.subplots_adjust(bottom=.3)
        plt.grid()
        plt.show()

    def get_property(self, propname):
        """Get property from history, sorted by status."""
        prop = self.history[propname].copy()
        status = self.history['status'].copy()
        #for stat in self.status_categories:
        #    print '\t%s' % stat
        index_of_first, index_of_last = \
            self.index_of_activation_and_deactivation()
        j = np.arange(status.shape[1])
        # Fill arrays with last value before deactivation
        for i in range(status.shape[0]):
            status[i, j > index_of_last[i]] = status[i, index_of_last[i]]
            prop[i, j > index_of_last[i]] = prop[i, index_of_last[i]]

        return prop.T, status.T

    def update_positions(self, x_vel, y_vel):
        """Move particles according to given velocity components.

        This method shall account for projection metrics (a distance
        on a map projection does not necessarily correspond to the same
        distance over true ground (not yet implemented).

        Arguments:
            x_vel and v_vel: floats, velocities in m/s of particle along
                             x- and y-axes of the inherit SRS (proj4).
        """

        geod = pyproj.Geod(ellps='WGS84')

        azimuth = np.degrees(np.arctan2(x_vel, y_vel))  # Direction of motion
        velocity = np.sqrt(x_vel**2 + y_vel**2)  # Velocity in m/s

        if not self.proj.is_latlong():  # Need to rotate SRS
            # Calculate x,y from lon,lat
            self.elements.x, self.elements.y = self.lonlat2xy(
                self.elements.lon, self.elements.lat)
            # Calculate azimuth orientation of y-axis at particle locations
            delta_y = 1000  # Using delta of 1000 m to calculate azimuth
            lon2, lat2 = self.xy2lonlat(self.elements.x,
                                        self.elements.y + delta_y)
            azimuth_srs = geod.inv(self.elements.lon, self.elements.lat,
                                   lon2, lat2)[0]
            azimuth = azimuth + azimuth_srs

        # Calculate new positions
        self.elements.lon, self.elements.lat, back_az = geod.fwd(
            self.elements.lon, self.elements.lat,
            azimuth, velocity*self.time_step.total_seconds())

        # Check that new positions are valid
        if (self.elements.lon.min() < -180) or (
                self.elements.lon.min() > 360) or (
                self.elements.lat.min() < -90) or (
                self.elements.lat.max() > 90):
            logging.info('Invalid new coordinates:')
            logging.info(self.elements)
            sys.exit('Quitting')

    def __repr__(self):
        """String representation providing overview of model status."""
        outStr = '===========================\n'
        outStr += 'Model:\t' + type(self).__name__ + '\n'
        outStr += '\t%s active %s particles  (%s deactivated, %s scheduled)\n'\
            % (self.num_elements_active(), self.ElementType.__name__,
               self.num_elements_deactivated(), self.num_elements_scheduled())
        outStr += 'Projection: %s\n' % self.proj4
        variable_groups, reader_groups, missing = self.get_reader_groups()
        outStr += '-------------------\n'
        outStr += 'Environment variables:\n'
        for i, variableGroup in enumerate(variable_groups):
            outStr += '  -----\n'
            readerGroup = reader_groups[i]
            for variable in sorted(variableGroup):
                outStr += '  ' + variable + '\n'
            for i, reader in enumerate(readerGroup):
                outStr += '     ' + str(i+1) + ') ' + reader + '\n'
        if len(self.missing_variables()) > 0:
            outStr += '  -----\n'
            outStr += 'Readers not added for the following variables:\n'
            for variable in sorted(self.missing_variables()):
                outStr += '  ' + variable + '\n'
        if hasattr(self, 'time'):
            outStr += 'Time:\n'
            outStr += '\tStart: %s\n' % (self.start_time)
            outStr += '\tPresent: %s\n' % (self.time)
            if hasattr(self, 'time_step'):
                outStr += '\tCalculation steps: %i * %s - total time: %s\n' % (
                    self.steps_calculation, self.time_step,
                    self.time-self.start_time)
                outStr += '\tOutput steps: %i * %s\n' % (
                    self.steps_output, self.time_step_output)
        if hasattr(self, 'history'):
            outStr += self.performance()
        outStr += '===========================\n'
        return outStr

    def add_halo_readers(self, path='/lustre/storeB/project/copernicus/sea/'):
        """Adding some Thredds and file readers in prioritised order"""

        readers = [  # Note that order (priority) is important!
            path + '/romsnorkyst/zdepths1h/NorKyst-800m_ZDEPTHS_his.fc*.nc',
            path + '/romsnordic/zdepths1h/roms_nordic4_ZDEPTHS_hr.fc*.nc',
            path + '/mywavewam4/mywavewam4.fc*.nc',
            'http://thredds.met.no/thredds/dodsC/sea/norkyst800m/1h/aggregate_be',
            'http://thredds.met.no/thredds/dodsC/sea/nordic4km/zdepths1h/aggregate_be',
            'http://thredds.met.no/thredds/dodsC/meps25files/meps_det_pp_2_5km_latest.nc',
            'http://thredds.met.no/thredds/dodsC/atmos/hirlam/hirlam12_be',
            'http://thredds.met.no/thredds/dodsC/sea/mywavewam4/mywavewam4_be']

        self.add_readers_from_list(readers, timeout=5)

    def _save_animation(self, anim, filename, fps):
        logging.info('Saving animation to ' + filename + '...')
        try:
            if filename[-4:] == '.gif':  # GIF
                logging.info('Making animated gif...')
                try:
                    anim.save(filename, fps=fps, writer='imagemagick')
                except:  # For old version of matplotlib
                    anim.save(filename, fps=fps, clear_temp=False)
                    os.system('convert -delay %i _tmp*.png %s' %
                            (np.abs(self.time_step_output.total_seconds())/
                             3600.*24., filename))
                    logging.info('Deleting temporary figures...')
                    tmp = glob.glob('_tmp*.png')
                    for tfile in tmp:
                        os.remove(tfile)
            else:  # MP4
                logging.info('Saving MP4 animation...')
                try:
                    anim.save(filename, fps=fps, bitrate=1800,
                              extra_args=['-pix_fmt', 'yuv420p'])
                except Exception as e:
                    logging.info(e)
                    try:
                        logging.info('Trying with codec libx264')
                        anim.save(filename, fps=fps, bitrate=1800,
                                  extra_args=['-pix_fmt', 'yuv420p',
                                              '-vcodec', 'libx264'])
                    except Exception as e:
                        logging.info(e)
                        anim.save(filename, fps=fps)
                        logging.warning('Animation might not be HTML5 compatible.')
        except Exception as e:
            logging.info('Could not save animation:')
            logging.info(e)
            logging.debug(traceback.format_exc())
                
