# Load CosmoMC format .dataset files for binned or unbinned CMB data
# AL July 2014 - Oct 2017
from __future__ import absolute_import
from __future__ import print_function
from matplotlib import pyplot as plt
import os
import numpy as np
import sys
import six
from getdist import IniFile, ParamNames
from scipy.linalg import sqrtm

try:
    sys.path.insert(0, 'c://work/dist/git/camb/')
    from camb.mathutils import chi_squared as fast_chi_squared

    del sys.path[0]
except:
    def fast_chi_squared(covinv, x):
        return covinv.dot(x).dot(x)


def lastTopComment(fname):
    result = None
    with open(fname) as f:
        x = f.readline()
        while x:
            x = x.strip()
            if x:
                if x[0] != '#': return result
                result = x[1:].strip()
            x = f.readline()
    return None


def readTextCommentColumns(fname, cols):
    incols = lastTopComment(fname).split()
    colnums = [incols.index(col) for col in cols]
    return np.loadtxt(fname, usecols=colnums, unpack=True)


def readWithHeader(fname):
    x = lastTopComment(fname)
    if not x:
        raise Exception('No Comment')
    return x.split(), np.loadtxt(fname)


class ClsArray(object):
    # Store arrays of cls: self.cls_array[i,j] is zero based array of correlation of field i with j

    def __init__(self, filename=None, camb_results=None, cols=None, field_names=['T', 'E', 'B', 'P'], rescale=None,
                 lmax=None):
        self.field_names = field_names
        self.cls_array = np.zeros((len(field_names), len(field_names)), dtype=np.object)
        self.cls_array[:, :] = None
        self.lmax = 0
        self.lmin = 0
        if filename is not None:
            assert (camb_results is None)
            self.loadFromFile(filename, cols)
        elif camb_results is not None:
            # use python camb
            lmax = camb_results._lmax_setting(lmax)
            totcl = camb_results.get_total_cls(lmax, CMB_unit='muK')
            self.lmin = 2
            self.lmax = totcl.shape[0] - 1
            self.cls_array[0, 0] = totcl[:, 0]
            self.cls_array[1, 1] = totcl[:, 1]
            self.cls_array[2, 2] = totcl[:, 2]
            self.cls_array[1, 0] = totcl[:, 3]
            self.cls_array[3, 3] = camb_results.get_lens_potential_cls(lmax)[:, 0]
        if rescale is not None:
            for i in range(4):
                for j in range(4):
                    if self.cls_array[i, j] is not None:
                        self.cls_array[i, j] *= rescale

    def loadFromFile(self, filename, cols=None, add_only=False):
        if cols is None:
            cols, dat = readWithHeader(filename)
        else:
            dat = np.loadtxt(filename)
            if isinstance(cols, six.string_types): cols = cols.split()
        Lix = cols.index('L')
        L = dat[:, Lix].astype(int)
        lmin = L[0]
        lmax = L[-1]
        if not add_only:
            self.lmin = lmin
            self.lmax = lmax
        for i, f in enumerate(self.field_names):
            for j, f2 in enumerate(self.field_names[:i + 1]):
                if add_only and self.cls_array[i, j] is not None:
                    print('Skipping columns already set %s%s' % (f, f2))
                    continue
                try:
                    ix = cols.index(f + f2)
                except:
                    try:
                        ix = cols.index(f2 + f)
                    except:
                        continue
                cls = np.zeros(max(self.lmax + 1, lmax + 1))
                cls[lmin:lmax + 1] = dat[:, ix]
                self.cls_array[i, j] = cls[:self.lmax + 1]

    def add_from_file(self, filename, cols=None):
        self.loadFromFile(filename, cols, add_only=True)

    def get(self, indices):
        i, j = indices
        if j > i: i, j = j, i
        return self.cls_array[i, j]


class BinWindows(object):
    def __init__(self, lmin, lmax, nbins):
        self.lmin = lmin
        self.lmax = lmax
        self.nbins = nbins

    def bin(self, TheoryCls, cls=None):
        if cls is None: cls = np.zeros((self.nbins, max([x for x in self.cols_out if x >= 0]) + 1))
        for i, ((x, y), ix_out) in enumerate(zip(self.cols_in.T, self.cols_out)):
            cl = TheoryCls[x, y]
            if cl is not None and ix_out >= 0:
                cls[:, ix_out] += np.dot(self.binning_matrix[i, :, :], cl.CL)
        return cls

    def write(self, froot, stem):
        if not os.path.exists(froot + stem + '_window'): os.mkdir(froot + '_window')
        for b in range(self.nbins):
            with open(froot + stem + '_window/window%u.dat' % (b + 1), 'w') as f:
                for L in np.arange(self.lmin[b], self.lmax[b] + 1):
                    f.write(("%5u " + "%10e" * len(self.cols_in) + "\n") % (L, self.binning_matrix[:, b, L]))


class DatasetLikelihood(object):
    def __init__(self, fname, dataset_params={}, field_names=['T', 'E', 'B', 'P'], map_separator='x'):
        self.field_names = field_names
        self.tot_theory_fields = len(field_names)
        self.map_separator = map_separator
        # aberration will be corrected if aberration_coeff is non - zero
        self.aberration_coeff = 0.0
        self.log_calibration_prior = -1  # if >0 use log prior on calibration parameter
        if '.dataset' in fname:
            self.loadDataset(fname, dataset_params)
        else:
            raise Exception('DatasetLikelihood only supports .dataset files')

    def typeIndex(self, field):
        return self.field_names.index(field)

    def PairStringToMapIndices(self, S):
        if len(S) == 2:
            if self.has_map_names:
                raise Exception('CMBlikes: CL names must use MAP1xMAP2 names')
            return self.map_names.index(S[0]), self.map_names.index(S[1])
        else:
            try:
                i = S.index(self.map_separator)
            except ValueError:
                raise ValueError('CMBLikes: invalid spectrum name %s' % S)
            return self.map_names.index(S[0:i]), self.map_names.index(S[i + 1:])

    def PairStringToUsedMapIndices(self, used_index, S):
        i1, i2 = self.PairStringToMapIndices(S)
        i1 = used_index[i1]
        i2 = used_index[i2]
        if i2 > i1:
            return i2, i1
        else:
            return i1, i2

    def UseString_to_cols(self, L):
        cl_i_j = self.UseString_to_Cl_i_j(L, self.map_used_index)
        cols = -np.ones(cl_i_j.shape[1], dtype=int)
        for i in range(cl_i_j.shape[1]):
            i1, i2 = cl_i_j[:, i]
            if i1 == -1 or i2 == -1: continue
            ix = 0
            for ii in range(self.nmaps):
                for jj in range(ii + 1):
                    if ii == i1 and jj == i2:
                        cols[i] = ix
                    ix += 1
        return cols

    def UseString_to_Cl_i_j(self, S, used_index):
        if not isinstance(S, (list, tuple)): S = S.split()
        cl_i_j = np.zeros((2, len(S)), dtype=int)
        for i, p in enumerate(S):
            cl_i_j[:, i] = self.PairStringToUsedMapIndices(used_index, p)
        return cl_i_j

    def MapPair_to_Theory_i_j(self, order, pair):
        i = self.map_fields[order[pair[0]]]
        j = self.map_fields[order[pair[1]]]
        if i <= j:
            return i, j
        else:
            return j, i

    def Cl_used_i_j_name(self, pair):
        return self.Cl_i_j_name(self.used_map_order, pair)

    def Cl_i_j_name(self, names, pair):
        name1 = names[pair[0]]
        name2 = names[pair[1]]
        if self.has_map_names:
            return name1 + self.map_separator + name2
        else:
            return name1 + name2

    def GetColsFromOrder(self, order):
        # Converts string Order = TT TE EE XY... or AAAxBBB AAAxCCC BBxCC
        # into indices into array of power spectra (and -1 if not present)
        cols = np.empty(self.ncl, dtype=int)
        cols[:] = -1
        names = order.strip().split()
        ix = 0
        for i in range(self.nmaps):
            for j in range(i + 1):
                name = self.Cl_used_i_j_name([i, j])
                if not name in names and i != j:
                    name = self.Cl_used_i_j_name([j, i])
                if name in names:
                    if cols[ix] != -1:
                        raise Exception('GetColsFromOrder: duplicate CL type')
                    cols[ix] = names.index(name)
                ix += 1
        return cols

    def elements_to_matrix(self, X, M):
        ix = 0
        for i in range(self.nmaps):
            M[i, 0:i] = X[ix:ix + i]
            M[0:i, i] = X[ix:ix + i]
            ix += i
            M[i, i] = X[ix]
            ix += 1

    def matrix_to_elements(self, M, X):
        ix = 0
        for i in range(self.nmaps):
            X[ix:ix + i + 1] = M[i, 0:i + 1]
            ix += i + 1

    def ReadClArr(self, ini, file_stem, return_full=False):
        # read file of CL or bins (indexed by L)
        filename = ini.relativeFileName(file_stem + '_file')
        cl = np.zeros((self.ncl, self.nbins_used))
        order = ini.string(file_stem + '_order', '')
        if not order:
            incols = lastTopComment(filename)
            if not incols:
                raise Exception('No column order given for ' + filename)
        else:
            incols = 'L ' + order
        cols = self.GetColsFromOrder(incols)
        data = np.loadtxt(filename)
        Ls = data[:, 0].astype(int)
        if self.binned: Ls -= 1
        for i, L in enumerate(Ls):
            if L >= self.bin_min and L <= self.bin_max:
                for ix in range(self.ncl):
                    if cols[ix] != -1:
                        cl[ix, L - self.bin_min] = data[i, cols[ix]]
        if L < self.bin_max:
            raise Exception('CMBLikes_ReadClArr: C_l file does not go up to maximum used: %s' % self.bin_max)
        if return_full:
            return incols.split(), data, cl
        else:
            return cl

    def readBinWindows(self, ini, file_stem):
        bins = BinWindows(self.pcl_lmin, self.pcl_lmax, self.nbins_used)
        in_cl = ini.split(file_stem + '_in_order')
        out_cl = ini.split(file_stem + '_out_order', in_cl)
        bins.cols_in = self.UseString_to_Cl_i_j(in_cl, self.map_required_index)
        bins.cols_out = self.UseString_to_cols(out_cl)
        norder = bins.cols_in.shape[1]
        if norder != bins.cols_out.shape[0]:
            raise Exception('_in_order and _out_order must have same number of entries')

        bins.binning_matrix = np.zeros((norder, self.nbins_used, self.pcl_lmax - self.pcl_lmin + 1))
        windows = ini.relativeFileName(file_stem + '_files')
        for b in range(self.nbins_used):
            window = np.loadtxt(windows % (b + 1 + self.bin_min))
            Err = False
            for i, L in enumerate(window[:, 0].astype(int)):
                if self.pcl_lmin <= L <= self.pcl_lmax:
                    bins.binning_matrix[:, b, L - self.pcl_lmin] = window[i, 1:]
                else:
                    Err = Err or any(window[i, 1:] != 0)
            if Err: print('WARNING: %s %u outside pcl_lmin-cl_max range: %s' % (file_stem, b, windows % (b + 1)))
        if ini.hasKey(file_stem + '_fix_cl_file'):
            raise Exception('fix_cl_file not implemented yet')
        return bins

    def init_map_cls(self, nmaps, order):
        if nmaps != len(order): raise ValueError('CMBLikes init_map_cls: size mismatch')

        class CrossPowerSpectrum(object):
            pass

        cls = np.empty((nmaps, nmaps), dtype=object)
        for i in range(nmaps):
            for j in range(i + 1):
                CL = CrossPowerSpectrum()
                cls[i, j] = CL
                CL.map_ij = [order[i], order[j]]
                CL.theory_ij = self.MapPair_to_Theory_i_j(order, [i, j])
                CL.CL = np.zeros(self.pcl_lmax - self.pcl_lmin + 1)
        return cls

    def loadDataset(self, froot, dataset_params):
        if not '.dataset' in froot: froot += '.dataset'
        ini = IniFile(froot)
        ini.params.update(dataset_params)
        self.readIni(ini)

    def readIni(self, ini):
        self.map_names = ini.split('map_names', default=[])
        self.has_map_names = len(self.map_names)
        if self.has_map_names:
            # e.g. have multiple frequencies for given field measurement
            map_fields = ini.split('map_fields')
            if len(map_fields) != len(self.map_names):
                raise Exception('CMBLikes: number of map_fields does not match map_names')
            self.map_fields = [self.typeIndex(f) for f in map_fields]
        else:
            self.map_names = self.field_names
            self.map_fields = np.arange(len(self.map_names), dtype=int)

        fields_use = ini.split('fields_use', [])
        if len(fields_use):
            index_use = [self.typeIndex(f) for f in fields_use]
            use_theory_field = [i in index_use for i in range(self.tot_theory_fields)]
        else:
            if not self.has_map_names:
                raise Exception('CMBlikes: must have fields_use or map_names')
            use_theory_field = [True] * self.tot_theory_fields

        maps_use = ini.split('maps_use', [])
        if len(maps_use):
            if np.any([not i for i in use_theory_field]):
                print('CMBlikes WARNING: maps_use overrides fields_use')
            self.use_map = np.zeros(len(self.map_names), dtype=bool)
            for j, map_used in enumerate(maps_use):
                if map_used in self.map_names:
                    self.use_map[self.map_names.index(map_used)] = True
                else:
                    raise ValueError('CMBlikes: maps_use item not found - %s' % map_used)
        else:
            self.use_map = [use_theory_field[self.map_fields[i]] for i in range(len(self.map_names))]

        # Bandpowers can depend on more fields than are actually used in likelihood
        # e.g. for correcting leakage or other linear corrections
        self.require_map = self.use_map[:]
        if self.has_map_names:
            if ini.hasKey('fields_required'):
                raise Exception('CMBLikes: use maps_required not fields_required')
            maps_use = ini.split('maps_required', [])
        else:
            maps_use = ini.split('fields_required', [])
        if len(maps_use):
            for j, map_used in enumerate(maps_use):
                if map_used in self.map_names:
                    self.require_map[self.map_names.index(map_used)] = True
                else:
                    raise ValueError('CMBlikes: required item not found %s' % map_used)

        self.required_theory_field = [False for _ in self.field_names]
        for i in range(len(self.map_names)):
            if self.require_map[i]:
                self.required_theory_field[self.map_fields[i]] = True

        self.ncl_used = 0  # set later reading covmat

        self.like_approx = ini.string('like_approx', 'gaussian')

        self.nmaps = np.count_nonzero(self.use_map)
        self.nmaps_required = np.count_nonzero(self.require_map)

        self.required_order = np.zeros(self.nmaps_required, dtype=int)
        self.map_required_index = -np.ones(len(self.map_names), dtype=int)
        ix = 0
        for i in range(len(self.map_names)):
            if self.require_map[i]:
                self.map_required_index[i] = ix
                self.required_order[ix] = i
                ix += 1

        self.map_used_index = -np.ones(len(self.map_names), dtype=int)
        ix = 0
        self.used_map_order = []
        for i, map_name in enumerate(self.map_names):
            if self.use_map[i]:
                self.map_used_index[i] = ix
                self.used_map_order.append(map_name)
                ix += 1

        self.ncl = (self.nmaps * (self.nmaps + 1)) // 2

        self.pcl_lmax = ini.int('cl_lmax')
        self.pcl_lmin = ini.int('cl_lmin')
        self.binned = ini.bool('binned', True)

        if self.binned:
            self.nbins = ini.int('nbins')
            self.bin_min = ini.int('use_min', 1) - 1
            self.bin_max = ini.int('use_max', self.nbins) - 1
            self.nbins_used = self.bin_max - self.bin_min + 1  # needed by readBinWindows
            self.bins = self.readBinWindows(ini, 'bin_window')
        else:
            if self.nmaps != self.nmaps_required:
                raise Exception('CMBlikes: unbinned likelihood must have nmaps==nmaps_required')
            self.nbins = self.pcl_lmax - self.pcl_lmin + 1
            if self.like_approx != 'exact':
                print('WARNING: Unbinned likelihoods untested in this version')
            self.bin_min = ini.int('use_min', self.pcl_lmin)
            self.bin_max = ini.int('use_max', self.pcl_lmax)
            self.nbins_used = self.bin_max - self.bin_min + 1

        self.full_bandpower_headers, self.full_bandpowers, self.bandpowers = \
            self.ReadClArr(ini, 'cl_hat', return_full=True)

        if self.like_approx == 'HL':
            self.cl_fiducial = self.ReadClArr(ini, 'cl_fiducial')
        else:
            self.cl_fiducial = None

        includes_noise = ini.bool('cl_hat_includes_noise', False)
        self.cl_noise = None
        if self.like_approx != 'gaussian' or includes_noise:
            self.cl_noise = self.ReadClArr(ini, 'cl_noise')
            if not includes_noise:
                self.bandpowers += self.cl_noise
            elif self.like_approx == 'gaussian':
                self.bandpowers -= self.cl_noise

        self.cl_lmax = np.zeros((self.tot_theory_fields, self.tot_theory_fields))
        for i in range(self.tot_theory_fields):
            if self.required_theory_field[i]: self.cl_lmax[i, i] = self.pcl_lmax
        if self.required_theory_field[0] and self.required_theory_field[1]:
            self.cl_lmax[1, 0] = self.pcl_lmax

        if self.like_approx != 'gaussian':
            cl_fiducial_includes_noise = ini.bool('cl_fiducial_includes_noise', False)

        self.bandpower_matrix = np.zeros((self.nbins_used, self.nmaps, self.nmaps))
        self.noise_matrix = self.bandpower_matrix.copy()
        self.fiducial_sqrt_matrix = self.bandpower_matrix.copy()
        if self.cl_fiducial is not None and not cl_fiducial_includes_noise:
            self.cl_fiducial += self.cl_noise

        for b in range(self.nbins_used):
            self.elements_to_matrix(self.bandpowers[:, b], self.bandpower_matrix[b, :, :])
            if self.cl_noise is not None:
                self.elements_to_matrix(self.cl_noise[:, b], self.noise_matrix[b, :, :])
            if self.cl_fiducial is not None:
                self.elements_to_matrix(self.cl_fiducial[:, b], self.fiducial_sqrt_matrix[b, :, :])
                self.fiducial_sqrt_matrix[b, :, :] = sqrtm(self.fiducial_sqrt_matrix[b, :, :])

        if self.like_approx == 'exact':
            self.fsky = ini.float('fullsky_exact_fksy')
        else:
            self.cov = self.ReadCovmat(ini)
            self.covinv = np.linalg.inv(self.cov)

        if 'linear_correction_fiducial_file' in ini.params:
            self.fid_correction = self.ReadClArr(ini, 'linear_correction_fiducial')
            self.linear_correction = self.readBinWindows(ini, 'linear_correction_bin_window')
        else:
            self.linear_correction = None

        if ini.hasKey('nuisance_params'):
            s = ini.relativeFileName('nuisance_params')
            self.nuisance_params = ParamNames(s)
            if ini.hasKey('calibration_param'):
                    raise Exception('calibration_param not allowed with nuisance_params')
            if ini.hasKey('calibration_paramname'):
                self.calibration_param = ini.string('calibration_paramname')
            else:
                self.calibration_param = None
        elif ini.string('calibration_param', ''):
            s = ini.relativeFileName('calibration_param')
            if not '.paramnames' in s:
                raise Exception('calibration_param must be paramnames file unless nuisance_params also specified')
            self.nuisance_params = ParamNames(s)
            self.calibration_param = self.nuisance_params.list()[0]
        else:
            self.calibration_param = None
        if self.calibration_param is not None:
            self.log_calibration_prior = ini.float('log_calibration_prior', -1)

        self.aberration_coeff = ini.float('aberration_coeff', 0.0)

        self.map_cls = self.init_map_cls(self.nmaps_required, self.required_order)

    def ReadCovmat(self, ini):
        # read the covariance matrix, and the array of which CL are in the covariance,
        # which then defines which set of bandpowers are used (subject to other restrictions)
        covmat_cl = ini.string('covmat_cl', allowEmpty=False)
        if ini.string('covmat_format', 'text') != 'text':
            raise Exception('Only text oovmat supported in python so far')
        self.full_cov = np.loadtxt(ini.relativeFileName('covmat_fiducial'))
        covmat_scale = ini.float('covmat_scale', 1.0)
        cl_in_index = self.UseString_to_cols(covmat_cl)
        self.ncl_used = np.sum(cl_in_index >= 0)
        self.cl_used_index = np.zeros(self.ncl_used, dtype=int)
        cov_cl_used = np.zeros(self.ncl_used, dtype=int)
        ix = 0
        for i, index in enumerate(cl_in_index):
            if index >= 0:
                self.cl_used_index[ix] = index
                cov_cl_used[ix] = i
                ix += 1
        if self.binned:
            num_in = len(cl_in_index)
            pcov = np.empty((self.nbins_used * self.ncl_used, self.nbins_used * self.ncl_used))
            for binx in range(self.nbins_used):
                for biny in range(self.nbins_used):
                    pcov[binx * self.ncl_used: (binx + 1) * self.ncl_used,
                    biny * self.ncl_used: (biny + 1) * self.ncl_used] = \
                        covmat_scale * self.full_cov[np.ix_((binx + self.bin_min) * num_in + cov_cl_used,
                                                            (biny + self.bin_min) * num_in + cov_cl_used)]

        else:
            raise Exception('unbinned covariance not implemented')
        return pcov

    def get_binned_theory(self, ClArray, data_params={}):
        # Useful for plotting, not used for likelihood
        self.get_theory_map_cls(ClArray, data_params)
        return self.get_binned_map_cls(self.map_cls)

    def get_full_bandpower_column(self, col_name):
        """
        Get columns from the input bandpower file. Note used by likelihood but may be useful for plotting.
        :param col_name: name of column, as in the top comment header of the file
        :return: column values
        """
        return self.full_bandpowers[:, self.full_bandpower_headers.index(col_name)]

    def diag_sigma(self):
        return np.sqrt(np.diag(self.full_cov))

    def plot(self, column='PP', ClArray=None, ls=None, ax=None):
        lbin = self.full_bandpowers[:, self.full_bandpower_headers.index('L_av')]

        binned_phicl_err = self.diag_sigma()
        ax = ax or plt.gca()
        bandpowers = self.full_bandpowers[:, self.full_bandpower_headers.index('PP')]
        if 'L_min' in self.full_bandpower_headers:
            lmin = self.full_bandpowers[:, self.full_bandpower_headers.index('L_min')]
            lmax = self.full_bandpowers[:, self.full_bandpower_headers.index('L_max')]
            ax.errorbar(lbin, bandpowers, yerr=binned_phicl_err, xerr=[lbin - lmin, lmax - lbin], fmt='o')
        else:
            ax.errorbar(lbin, bandpowers, yerr=binned_phicl_err, fmt='o')

        if ClArray is not None:
            if isinstance(ClArray, ClsArray):
                i, j = self.MapPair_to_Theory_i_j(range(len(self.map_names)), self.PairStringToMapIndices(column))
                ClArray = ClArray.get([i, j])
            if ls is None: ls = np.arange(len(ClArray))
            ax.plot(ls, ClArray, color='k')
            ax.set_xlim([2, ls[-1]])

    def get_binned_map_cls(self, Cls, corrections=True):
        band = self.bins.bin(Cls)
        if self.linear_correction is not None and corrections:
            band += self.linear_correction.bin(Cls) - self.fid_correction.T
        return band

    def get_theory_map_cls(self, ClArray, data_params={}):
        for i in range(self.nmaps_required):
            for j in range(i + 1):
                CL = self.map_cls[i, j]
                cls = ClArray.get(CL.theory_ij)
                if cls is not None:
                    CL.CL[:] = cls[self.pcl_lmin:self.pcl_lmax + 1]
                else:
                    CL.CL[:] = 0

        self.adapt_theory_for_maps(self.map_cls, data_params)

    def adapt_theory_for_maps(self, cls, data_params):
        if self.aberration_coeff: self.add_aberration(cls)
        self.add_foregrounds(cls, data_params)
        if self.calibration_param is not None and self.calibration_param in data_params:
            for i in range(self.nmaps_required):
                for j in range(i + 1):
                    CL = cls[i, j]
                    if CL is not None:
                        if CL.theory_ij[0] <= 2 and CL.theory_ij[1] <= 2:
                            CL.CL /= data_params[self.calibration_param] ** 2

    def add_foregrounds(self, cls, data_params):
        pass

    def add_aberration(self, cls):
        # adapted from CosmoMC function by Christian Reichardt
        ells = np.arange(self.pcl_lmin, self.pcl_lmax + 1)
        cl_norm = ells * (ells + 1)
        for i in range(self.nmaps_required):
            for j in range(i + 1):
                CL = cls[i, j]
                if CL is not None:
                    if CL.theory_ij[0] <= 2 and CL.theory_ij[1] <= 2:
                        # first get Cl instead of Dl
                        cl_deriv = CL.CL / cl_norm
                        # second take derivative dCl/dl
                        cl_deriv[1:-1] = (cl_deriv[2:] - cl_deriv[:-2]) / 2
                        # handle endpoints approximately
                        cl_deriv[0] = cl_deriv[1]
                        cl_deriv[-1] = cl_deriv[-2]
                        # reapply to Dl's.
                        # note never took 2pi out, so not putting it back either
                        cl_deriv *= cl_norm
                        # also multiply by ell since really wanted ldCl/dl
                        cl_deriv *= ells
                        CL.CL += self.aberration_coeff * cl_deriv

    def write_likelihood_data(self, filename, data_params={}):
        cls = self.init_map_cls(self.nmaps_required, self.required_order)
        self.add_foregrounds(cls, data_params)
        with open(filename, 'w') as f:
            cols = []
            for i in range(self.nmaps_required):
                for j in range(i + 1):
                    cols.append(self.Cl_i_j_name(self.map_names, cls[i, j].map_ij))
            f.write('#    L' + ("%17s " * len(cols)) % tuple(cols) + '\n')
            for b in range(self.pcl_lmin, self.pcl_lmax + 1):
                c = [b]
                for i in range(self.nmaps_required):
                    for j in range(i + 1):
                        c.append(cls[i, j].CL[b - self.pcl_lmin])
                f.write(("%I5 " + "%17.8e " * len(cols)) % tuple(c))

    def transform(self, C, Chat, Cfhalf):
        # HL transformation of the matrices
        if C.shape[0] == 1:
            rat = Chat[0, 0] / C[0, 0]
            C[0, 0] = np.sign(rat - 1) * np.sqrt(2 * np.maximum(0, rat - np.log(rat) - 1)) * Cfhalf[0, 0] ** 2
            return
        diag, U = np.linalg.eigh(C)
        rot = U.T.dot(Chat).dot(U)
        roots = np.sqrt(diag)
        for i, root in enumerate(roots):
            rot[i, :] /= root
            rot[:, i] /= root
        U.dot(rot.dot(U.T), rot)
        diag, rot = np.linalg.eigh(rot)
        diag = np.sign(diag - 1) * np.sqrt(2 * np.maximum(0, diag - np.log(diag) - 1))
        Cfhalf.dot(rot, U)
        for i, d in enumerate(diag):
            rot[:, i] = U[:, i] * d
        rot.dot(U.T, C)

    def exact_chi_sq(self, C, Chat, L):
        if C.shape[0] == 1:
            return (2 * L + 1) * self.fsky * (Chat[0, 0] / C[0, 0] - 1 - np.log(Chat[0, 0] / C[0, 0]))
        else:
            M = np.linalg.inv(C).dot(Chat)
            return (2 * L + 1) * self.fsky * (np.trace(M) - self.nmaps - np.linalg.slogdet(M)[1])

    def chi_squared(self, ClArray, data_params={}, return_binned_theory=False):

        self.get_theory_map_cls(ClArray, data_params)

        C = np.empty((self.nmaps, self.nmaps))
        bigX = np.empty(self.nbins_used * self.ncl_used)
        vecp = np.empty(self.ncl)
        chisq = 0

        if self.binned:
            binned_theory = self.get_binned_map_cls(self.map_cls)
        else:
            Cs = np.zeros((self.nbins_used, self.nmaps, self.nmaps))
            for i in range(self.nmaps):
                for j in range(i + 1):
                    CL = self.map_cls[i, j]
                    if CL is not None:
                        Cs[:, i, j] = CL.CL[self.bin_min - self.pcl_lmin:self.bin_max - self.pcl_lmin + 1]
                        Cs[:, j, i] = CL.CL[self.bin_min - self.pcl_lmin:self.bin_max - self.pcl_lmin + 1]

        for bin in range(self.nbins_used):
            if self.binned:
                self.elements_to_matrix(binned_theory[bin, :], C)
            else:
                C[:, :] = Cs[bin, :, :]

            if self.cl_noise is not None:
                C += self.noise_matrix[bin]

            if self.like_approx == 'exact':
                chisq += self.exact_chi_sq(C, self.bandpower_matrix[bin], self.bin_min + bin)
                continue
            elif self.like_approx == 'HL':
                self.transform(C, self.bandpower_matrix[bin], self.fiducial_sqrt_matrix[bin])
            elif self.like_approx == 'gaussian':
                C -= self.bandpower_matrix[bin]

            self.matrix_to_elements(C, vecp)

            bigX[bin * self.ncl_used:(bin + 1) * self.ncl_used] = vecp[self.cl_used_index]

        if self.like_approx != 'exact':
            chisq = fast_chi_squared(self.covinv, bigX)

        if self.log_calibration_prior > 0:
            chisq += (np.log(data_params[self.calibration_param]) / self.log_calibration_prior) ** 2
        if return_binned_theory and self.binned:
            return chisq, binned_theory
        else:
            return chisq


def plotAndChisq(dataset, cl_file, data_params={}):
    d = DatasetLikelihood(dataset)
    cls = ClsArray(cl_file)
    d.plot(cls)
    print('Chi-squared: ', d.chi_squared(cls, data_params))
    plt.show()


if __name__ == "__main__":
    #    plotAndChisq(r'test_data/g60_full_pp.dataset', r'test_data/testout_pp.theory_cl')
    #    sys.exit()
    try:
        import argparse
    except:
        print('use "module load" to load python 2.7')
        sys.exit()
    parser = argparse.ArgumentParser(description="Load .dataset and calculate likelihood")
    parser.add_argument('dataset', help='.dataset filename')
    parser.add_argument('cl_file', help='file of Cls')
    args = parser.parse_args()
    plotAndChisq(args.dataset, args.cl_file)
