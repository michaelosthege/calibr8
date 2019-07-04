import abc
import logging
logger = logging.getLogger('calibr8.contrib.glucose')
import numpy  
import scipy.optimize
try:
    import pymc3 as pm
except ModuleNotFoundError:  # pymc3 is optional, throw exception when used
    class _ImportWarner:
        __all__ = []

        def __init__(self, attr):
            self.attr = attr

        def __call__(self, *args, **kwargs):
            raise ImportError(
                "PyMC3 is not installed. In order to use this function:\npip install pymc3"
            )

    class _PyMC3:
        def __getattr__(self, attr):
            return _ImportWarner(attr)
    
    pm = _PyMC3()

try:
    import theano
except ModuleNotFoundError:  # theano is optional, throw exception when used

    class _ImportWarnerTheano:
        __all__ = []

        def __init__(self, attr):
            self.attr = attr

        def __call__(self, *args, **kwargs):
            raise ImportError(
                "Theano is not installed. In order to use this function:\npip install theano"
            )

    class _Theano:
        def __getattr__(self, attr):
            return _ImportWarnerTheano(attr)
    
    theano = _Theano()

from .. core import ErrorModel, asymmetric_logistic, inverse_asymmetric_logistic, polynomial


class BaseGlucoseErrorModel(ErrorModel):
    def __init__(self, independent_key:str, dependent_key:str):
        """ A class for modeling the error of OD measurements of glucose.

        Args:
            independent: glucose concentration
            dependent: OD measurements
        """
        super().__init__(independent_key, dependent_key)
        self.student_df = 1


    def loglikelihood(self, *, y_obs,  y_hat, theta=None):
        """Loglikelihood of observation (dependent variable) given the independent variable

        Args:
            y_obs (array): observed backscatter measurements (dependent variable)
            y_hat (array): predicted values of independent variable
            theta: parameters describing the logistic function of mu and the polynomial function of sigma 
                   (to be fitted with data, otherwise theta=self.theta_fitted)
        
        Return:
            Sum of loglikelihoods

        """
        if theta is None:
            if self.theta_fitted is None:
                raise Exception('No parameter vector was provided and the model is not fitted with data yet.')
            theta = self.theta_fitted

        mu, sigma, df = self.predict_dependent(y_hat, theta=theta)
        # using t-distributed error in the non-transformed space
        likelihoods = scipy.stats.t.pdf(x=y_obs, loc=mu, scale=sigma, df=df)
        loglikelihoods = numpy.log(likelihoods)
        ll = numpy.sum(loglikelihoods)
        return ll


class LinearGlucoseErrorModel(BaseGlucoseErrorModel):
    def linear(self, y_hat, theta_lin):
        """Linear model of the expected measurement outcomes, given a true independent variable.
        
        Arguments:
            y_hat (array): realizations of the independent variable
            theta_lin (array): parameters of the linear model
        """
        return theta_lin[0] + theta_lin[1] * y_hat
    
    def constant(self, y_hat, theta_con):
        """Constant model for the width of the error distribution
        
        Arguments:
            y_hat (array): realizations of the independent variable
            theta_con (array): parameters of the constant model
        """
        return theta_con + 0 * y_hat

    def predict_dependent(self, y_hat, *, theta=None):
        """Predicts the parameters mu and sigma of a student-t-distribution which characterises the dependent variable (OD) 
           given values of the independent variable (Glucose concentration).

        Args:
            y_hat (array): values of the independent variable
            theta: parameters describing the linear function of mu and the constant function of sigma (default to self.theta_fitted)

        Returns:
            mu,sigma (array): values for mu and sigma characterising the student-t-distributions describing the dependent variable (OD)
            df: degree of freedom of student-t-distribution (always set to 1)
        """
        if theta is None:
            theta = self.theta_fitted
        mu = self.linear(y_hat, theta[:2])
        sigma = self.constant(y_hat, theta[2:])
        df = 1
        return mu, sigma, df

    def predict_independent(self, y_obs):
        """Predict the most likely value of the independent variable using the calibrated error model in inverse direction.

        Args:
            y_obs (array): observed backscatter measurements

        Returns:
            mu (array): predicted glucose concentrations given the observations
        """
        a, b, sigma = self.theta_fitted
        mu = (y_obs - a) / b
        return mu
    
    def infer_independent(self, y_obs, *, glc_lower=0, glc_upper=100, draws=1000):
        """Infer the posterior distribution of the independent variable given the observations of one point of the dependent variable.
        
        Args:
            y_obs (array): observed OD measurements
            glc_lower (int): lower limit for uniform distribution of glucose prior
            glc_upper (int): lower limit for uniform distribution of glucose prior
            draws (int): number of samples to draw (handed to pymc3.sample)
        
        Returns:
            trace: trace of the posterior distribution of inferred glucose concentration
        """ 
        theta = self.theta_fitted
        with pm.Model() as model:
            glc = pm.Uniform('Glucose', lower=glc_lower, upper=glc_upper, shape=(1,))
            mu, sd, df = self.predict_dependent(glc, theta=theta)
            ll = pm.StudentT('likelihood', nu=df, mu=mu, sd=sd, observed=y_obs, shape=(1,))
            trace = pm.sample(draws)
        return trace


class LogisticGlucoseErrorModel(BaseGlucoseErrorModel):
    def predict_dependent(self, y_hat, *, theta=None):
        """Predicts the parameters mu and sigma of a student-t-distribution which characterises the dependent variable given values of the independent variable.

        Args:
            y_hat (array): values of the independent variable
            theta: parameters describing the logistic function of mu and the polynomial function of sigma (default to self.theta_fitted)

        Returns:
            mu, scale, df (array): values for mu, scale, df charcterising the student-t-distributions describing the dependent variable
        """
        if theta is None:
            theta = self.theta_fitted
        mu = asymmetric_logistic(y_hat, theta[:5])
        sigma = polynomial(mu, theta[5:])
        df = self.student_df
        return mu, sigma, df

    def predict_independent(self, y_obs):
        """Predict the most likely value of the independent variable using the calibrated error model in inverse direction.

        Args:
            y_obs (array): observed absorbance measurements (dependent variable)

        Returns:
            y_hat (array): most likely glucose values (independent variable)
        """
        y_hat = inverse_asymmetric_logistic(y_obs, self.theta_fitted)
        return y_hat
    
    def theano_asymmetric_logistic(self, y_hat, theta):
        """5-parameter logistic model of the expected measurement outcome, given a true independent variable.
    
        Args:
            y_hat (array): realizations of the independent variable
            theta (array): parameters of the logistic model
            L_L: lower asymptote
            L_U: upper asymptote
            k: growth rate
            I_x: x-value at inflection point
            v: parameter affecting the position of the inflection point (symmetry)
        
        Returns:
            y_val(array): expected measurement outcome
        """
        L_L, L_U, I_x, k, v = theta[:5]
        y_val = L_L + (L_U-L_L)/(theano.tensor.power((1+theano.tensor.exp(-k*(y_hat-I_x))),1/v))

        return y_val

    def infer_independent(self, y_obs, *, glc_lower=0, glc_upper=100, draws=1000):
        """Infer the posterior distribution of the independent variable given the observations of one point of the dependent variable.
        
        Args:
            y_obs (array): observed OD measurements
            glc_lower (int): lower limit for uniform distribution of glucose prior
            glc_upper (int): lower limit for uniform distribution of glucose prior
            draws (int): number of samples to draw (handed to pymc3.sample)
        
        Returns:
            trace: trace of the posterior distribution of inferred glucose concentration
        """ 
        theta = self.theta_fitted
        with pm.Model() as model:
            glc = pm.Uniform('Glucose', lower=glc_lower, upper=glc_upper, shape=(1,))
            mu = self.theano_asymmetric_logistic(glc, theta[:5])
            sd = polynomial(glc, theta[5:])
            ll = pm.StudentT('likelihood', nu=self.student_df, mu=mu, sd=sd, observed=y_obs, shape=(1,))
            trace = pm.sample(draws)
        return trace
