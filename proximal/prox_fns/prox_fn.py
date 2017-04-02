from __future__ import division
import abc
import tempfile
import os.path
import numpy as np
from proximal.utils import Impl
from proximal.utils import matlab_support
from proximal.utils.codegen import sub2ind, ind2sub, indent, replace_local_floats_with_double

import pycuda.driver as cuda
import pycuda.autoinit
from pycuda.compiler import SourceModule
from pycuda import gpuarray
import pycuda.tools

class ProxFn(object):
    """Represents alpha*f(beta*x - b) + <c,x> + gamma*<x,x> + d
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, lin_op, alpha=1.0, beta=1.0, b=0.0, c=0.0,
                 gamma=0.0, d=0.0, implem=None):
        # Error checking.
        for elem, name in zip([b, c], ["b", "c"]):
            if not (np.isscalar(elem) or elem.shape == lin_op.shape):
                raise Exception("Invalid dimensions of %s." % name)
        for elem, name in zip([alpha, gamma, d], ["alpha", "gamma"]):
            if not np.isscalar(elem) or elem < 0:
                raise Exception("%s must be a nonnegative scalar." % name)
        for elem, name in zip([beta, d], ["beta", "d"]):
            if not np.isscalar(elem):
                raise Exception("%s must be a scalar." % name)

        self.implem_key = implem
        self.implementation = Impl['numpy']
        if implem is not None:
            self.set_implementation(implem)

        self.lin_op = lin_op
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.b = b
        self.c = c
        if np.isscalar(b):
            self.b = b * np.ones(self.lin_op.shape)
        if np.isscalar(c):
            self.c = c * np.ones(self.lin_op.shape)
        self.gamma = float(gamma)
        self.d = float(d)
        self.init_tmps()
        self.kernel_cuda_prox = None
        super(ProxFn, self).__init__()

    def set_implementation(self, im):
        if im in Impl.values():
            self.implementation = im
        elif im in Impl.keys():
            self.implementation = Impl[im]
        else:
            raise Exception("Invalid implementation.")

        return self.implementation

    def implementation(self, im):
        return self.implementation

    def variables(self):
        """Return a list of the variables in the problem.
        """
        return self.lin_op.variables()

    def init_tmps(self):
        """Initialize temporary variables for _prox method.
        """
        pass

    @abc.abstractmethod
    def _prox(self, rho, v, *args, **kwargs):
        """The prox function for a specific atom.
        """
        return NotImplemented

    def prox(self, rho, v, *args, **kwargs):
        """Wrapper on the prox function to handle alpha, etc.
           It is here the iteration for debug purposese etc.
        """
        rho_hat = (rho + 2 * self.gamma) / (self.alpha * self.beta**2)
        # vhat = (rho*v - c)*beta/(rho + 2*gamma) - b
        # Modify v in-place. This is important for the Python to be performant.
        v *= rho
        v -= self.c
        v *= self.beta / (rho + 2 * self.gamma)
        v -= self.b
        xhat = self._prox(rho_hat, v, *args, **kwargs)
        # x = (xhat + b)/beta
        # Modify result in-place.
        xhat += self.b
        xhat /= self.beta
        return xhat

    def cuda_additional_buffers(self):
        res = []
        if not np.all(self.c == 0):
            res.append( ("c", self.c) )
        if not np.all(self.b == 0):
            res.append( ("b", self.b) )
        return res
    
    def gen_cuda_code(self):
        shape = self.lin_op.shape
        dimv = int(np.prod(shape))
        self.cuda_args = []
        argnames = []
        for aname,aval in self.cuda_additional_buffers():
            self.argnames.append(aname)
            aval = gpuarray.to_gpu(aval.astype(np.float32))
            self.cuda_args.append( aname, aval )
        argstring = "".join( ", const float *%s" % arg for arg in argnames)
        ccode = "- c[%(idx)s]" if "c" in argnames else ""
        bcode1 = " - b[%(idx)s]" if "b" in argnames else ""
        bcode2 = "xhatc += b[vidx];\n" if "b" in argnames else ""
        if self.gamma == 0:
            plus_2gamma = ""
        else:
            plus_2gamma = " + %.8ef" % (self.gamma * 2)
        alpha_beta_s = self.alpha * self.beta**2;
        if alpha_beta_s == 1:
            div_alpha_beta_s = ""
        else:
            div_alpha_beta_s = " / %.8ef" % (alpha_beta_s)
        beta = self.beta
        if beta == 1:
            xhatc_div_beta = ""
            mul_beta = ""
        else:
            xhatc_div_beta = "xhatc /= %.8ef;" % beta
            mul_beta = " * %.8ef" % beta
        
        gen_v = lambda idx: "(((v[%(idx)s] * rho)%(ccode)s)%(mul_beta)s) / (rho%(plus_2gamma)s%(bcode1)s)" % dict(
                    idx=sub2ind(idx, shape) if len(idx) > 1 else idx[0],
                    ccode=ccode % locals(),
                    mul_beta=mul_beta,
                    plus_2gamma=plus_2gamma,
                    bcode1=bcode1 % locals(),
                )
        
        
        cucode = self._prox_cuda("rho_hat", gen_v, ind2sub("vidx", shape), "vidx", "xhatc")
        cucode = indent(cucode, 8)
        
        code = """
__global__ void prox(const float *v, float *xhat, float rho%(argstring)s)
{
    float rho_hat = (rho%(plus_2gamma)s)%(div_alpha_beta_s)s;
    
    int index = blockIdx.x * blockDim.x + threadIdx.x; 
    int stride = blockDim.x * gridDim.x;
    for( int vidx = index; vidx < %(dimv)d; vidx += stride )
    {
        %(cucode)s
        %(bcode2)s
        %(xhatc_div_beta)s

        xhat[vidx] = xhatc;
    }
}
""" % locals()
        #print(code)
        try:
            self.cuda_code = code if 1 else replace_local_floats_with_double(code)
            mod = SourceModule(code)
        except cuda.CompileError as e:
            print(code)
            print("CUDA compilation error:")
            print(e.stderr)
            raise e
        cuda_func_prox = mod.get_function("prox")            
        block = (min(int(np.prod(shape)), cuda_func_prox.MAX_THREADS_PER_BLOCK), 1, 1)
        grid = (int(np.prod(shape))//block[0],1,1)
        const_vals = tuple(x[1] for x in self.cuda_args)
        if 0:
            prepared_prox = cuda_func_prox.prepare("PPf" + "P"*len(const_vals))
            self.kernel_cuda_prox = lambda *args: prepared_prox.prepared_timed_call(grid, block, *(x.gpudata for x in (args+const_vals)))()
        else:
            self.kernel_cuda_prox = lambda *args: cuda_func_prox(*(args+const_vals), grid=grid, block=block, time_kernel=True)
        
        
    def prox_cuda(self, rho, v, *args, **kwargs):
        if hasattr(self, "_prox_cuda"):
            if self.kernel_cuda_prox is None:
                self.gen_cuda_code()
            if not type(v) == gpuarray.GPUArray:
                v = gpuarray.to_gpu(v.astype(np.float32))
            xhat = gpuarray.zeros(v.shape, dtype=np.float32)
            self.kernel_cuda_prox(v, xhat, np.float32(rho))
            return xhat
        else:
            c = gpuarray.to_gpu(self.c)
            b = gpuarray.to_gpu(self.b)
            cuda_fun = lambda rho, v, *args, **kw: gpuarray.to_gpu(self._prox(rho, v.get(), *args, **kw))            
            rho_hat = (rho + 2 * self.gamma) / (self.alpha * self.beta**2)
            # vhat = (rho*v - c)*beta/(rho + 2*gamma) - b
            # Modify v in-place. This is important for the Python to be performant.
            v *= rho
            v -= c
            v *= self.beta / (rho + 2 * self.gamma)
            v -= b
            xhat = cuda_fun(rho_hat, v, *args, **kwargs)
            # x = (xhat + b)/beta
            # Modify result in-place.
            xhat += b
            xhat /= self.beta
            return xhat        
    
    def _init_matlab(self, prefix):
        return ""
        
    @abc.abstractmethod
    def _prox_matlab(self, prefix, output_var, rho_var, v_var, *args, **kwargs):
        """Returns matlab code for this prox function"""
        return NotImplemented
        
    @abc.abstractmethod
    def _eval_matlab(self, prefix, output_var, v_var):
        """Returns matlab code for this prox function"""
        return NotImplemented

    def init_matlab(self, prefix):
        res = """
global %(prefix)s_proxint_alpha %(prefix)s_proxint_beta %(prefix)s_proxint_gamma %(prefix)s_proxint_b %(prefix)s_proxint_c %(prefix)s_proxint_d
obj.d.%(prefix)s_proxint_alpha = %(prefix)s_proxint_alpha;
obj.d.%(prefix)s_proxint_beta = %(prefix)s_proxint_beta;
obj.d.%(prefix)s_proxint_gamma = %(prefix)s_proxint_gamma;
obj.d.%(prefix)s_proxint_d = gpuArray(%(prefix)s_proxint_d);
""" % locals()
        matlab_support.put_array(prefix + "_proxint_alpha", np.array(self.alpha, np.float32), globalvar = True)
        matlab_support.put_array(prefix + "_proxint_beta", np.array(self.beta, np.float32), globalvar = True)
        matlab_support.put_array(prefix + "_proxint_gamma", np.array(self.gamma, np.float32), globalvar = True)
        if not np.all(self.b == 0):
            matlab_support.put_array(prefix + "_proxint_b", np.array(self.b, np.float32), globalvar = True)
            res += "obj.d.%(prefix)s_proxint_b = gpuArray(%(prefix)s_proxint_b);\n" % locals()
        if not np.all(self.c == 0):
            matlab_support.put_array(prefix + "_proxint_c", np.array(self.c, np.float32), globalvar = True)
            res += "obj.d.%(prefix)s_proxint_c = gpuArray(%(prefix)s_proxint_c);\n" % locals()
        matlab_support.put_array(prefix + "_proxint_d", np.array(self.d, np.float32), globalvar = True)
        res += self._init_matlab(prefix)
        return res
        
    def prox_matlab(self, prefix, output_var, rho_var, v_var, *args, **kwargs):
        if np.all(self.c == 0):
            minus_c = ''
        else:
            minus_c = ' - obj.d.%(prefix)s_proxint_c' % locals()
            
        if np.all(self.b == 0):
            minus_b = ''
            plus_b = ''
        else:
            minus_b = ' - obj.d.%(prefix)s_proxint_b' % locals()
            plus_b = ' + obj.d.%(prefix)s_proxint_b' % locals()
        
        if self.beta == 1:
            div_beta = ''
        else:
            div_beta = ' / obj.d.%(prefix)s_proxint_beta' % locals()
        
        res = """
rho_hat = (%(rho_var)s + 2 * obj.d.%(prefix)s_proxint_gamma) / (obj.d.%(prefix)s_proxint_alpha * obj.d.%(prefix)s_proxint_beta^2);
v_hat = ((%(v_var)s * %(rho_var)s)%(minus_c)s) * (obj.d.%(prefix)s_proxint_beta / (%(rho_var)s + 2 * obj.d.%(prefix)s_proxint_gamma ) )%(minus_b)s;
""" % locals()
        res += self._prox_matlab(prefix, "x_hat", "rho_hat", "v_hat", *args, **kwargs)
        res += """
%(output_var)s = (x_hat%(plus_b)s)%(div_beta)s;
""" % locals()
        return res

    def eval_matlab(self, prefix, v_var):
        if np.all(self.c == 0):
            cdotv = ''
        else:
            cdotv = 'dot(obj.d.%(prefix)s_proxint_c(:), %(v_var)s(:)) +' % locals()
            
        if np.all(self.b == 0):
            minus_b = ''
            plus_b = ''
        else:
            minus_b = ' - obj.d.%(prefix)s_proxint_b' % locals()
            plus_b = ' + obj.d.%(prefix)s_proxint_b' % locals()

        res = """
tmp = %(v_var)s * obj.d.%(prefix)s_proxint_beta%(minus_b)s;
""" %locals()
        res += self._eval_matlab(prefix, "evp", "tmp")
        res += """
evp = gather(obj.d.%(prefix)s_proxint_alpha * evp + %(cdotv)s obj.d.%(prefix)s_proxint_gamma * sum(reshape(%(v_var)s.^2, 1, [])) + obj.d.%(prefix)s_proxint_d);
""" % locals()
        return res

    def genmatlab(self, prefix, mlclass):
        init_code = self.init_matlab(prefix)
        mlclass.add_method("""
function obj = %(prefix)s_init(obj)
    %(init_code)s
end
""" % locals(), constructor="obj = obj.%(prefix)s_init();" % locals())
        
        self.matlab_prox_script = "%(prefix)s_prox" % locals()
        prox_code = self.prox_matlab(prefix, "prox_out", "rho", "prox_in")
        mlclass.add_method("""
function prox_out = %(prefix)s_prox(obj, prox_in, rho)
    %(prox_code)s
end
""" % locals())
        
        self.matlab_eval_script = "%(prefix)s_eval" % locals()
        eval_code = self.eval_matlab(prefix, "prox_eval_in")
        mlclass.add_method("""
function evp = %(prefix)s_eval(obj, prox_eval_in)
    %(eval_code)s
end
""" % locals())
        
    @abc.abstractmethod
    def _eval(self, v):
        """Evaluate the function on v (ignoring parameters).
        """
        return NotImplemented

    def eval(self, v):
        """Evaluate the function on v.
        """
        return self.alpha * self._eval(self.beta * v - self.b) + \
            np.sum(self.c * v) + self.gamma * np.square(v).sum() + self.d

    @property
    def value(self):
        return self.eval(self.lin_op.value)

    def __str__(self):
        """Default to string is name of class.
        """
        #Represents alpha*f(beta*x - b) + <c,x> + gamma*<x,x> + d
        r = ""
        if self.alpha != 1.: r += "%02.03e * " % self.alpha
        r += self.__class__.__name__ + "( "
        if self.beta != 1.: r += "%02.03e * " % self.beta
        r += "x"
        if not np.alltrue(np.ravel(self.b) == 0.0): r += " - b"
        r += " )"
        if not np.all(self.c == 0.0): r += " + <c,x>"
        if not self.gamma == 0.0: r += " + %02.03e * <x,x>" % self.gamma
        if not self.d == 0.0: r += " + %02.03e" % self.d
        r += "; x = " + str(self.lin_op)
        return "{ " + r + " }"

    def __add__(self, other):
        """ProxFn + ProxFn(s).
        """
        if isinstance(other, ProxFn):
            return [self, other]
        elif type(other) == list:
            return [self] + other
        else:
            return NotImplemented

    def __radd__(self, other):
        """Called for list + ProxFn.
        """
        if type(other) == list:
            return other + [self]
        else:
            return NotImplemented

    def __mul__(self, other):
        """ProxFn * Number.
        """
        # Can only multiply by scalar constants.
        if np.isscalar(other) and other > 0:
            return self.copy(alpha=self.alpha * other)
        else:
            raise TypeError("Can only multiply by a positive scalar.")

    def __rmul__(self, other):
        """Called for Number * ProxFn.
        """
        return self * other

    def __div__(self, other):
        """Called for ProxFn/Number.
        """
        return (1. / other) * self

    def __truediv__(self, other):
        """ProxFn / integer.
        """
        return self.__div__(other)

    def copy(self, lin_op=None, **kwargs):
        """Returns a shallow copy of the object.

        Used to reconstruct an object tree.

        Parameters
        ----------
        args : list, optional
            The arguments to reconstruct the object. If args=None, use the
            current args of the object.

        Returns
        -------
        Expression
        """
        if lin_op is None:
            lin_op = self.lin_op
        data = self.get_data()
        curr_args = {'alpha': self.alpha,
                     'beta': self.beta,
                     'gamma': self.gamma,
                     'c': self.c,
                     'b': self.b,
                     'd': self.d,
                     'implem': self.implem_key}
        for key in curr_args.keys():
            if key not in kwargs:
                kwargs[key] = curr_args[key]
        return type(self)(lin_op, *data, **kwargs)

    def get_data(self):
        """Returns info needed to reconstruct the object besides the args.

        Returns
        -------
        list
        """
        return []
