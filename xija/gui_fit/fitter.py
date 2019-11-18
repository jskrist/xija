import sherpa.ui as ui

import pyyaks.logger
import logging
import multiprocessing as mp
import time
import numpy as np

logging.basicConfig(level=logging.INFO)

fit_logger = pyyaks.logger.get_logger(name='fit', level=logging.INFO,
                                      format='[%(levelname)s] (%(processName)-10s) %(message)s')

# Default configurations for fit methods
sherpa_configs = dict(
    simplex = dict(ftol=1e-3,
                   finalsimplex=0,   # converge based only on length of simplex
                   maxfev=1000),
)


class FitTerminated(Exception):
    pass


class CalcModel(object):
    def __init__(self, model):
        self.model = model

    def __call__(self, parvals, x):
        """This is the Sherpa calc_model function, but in this case calc_model does not
        actually calculate anything but instead just stores the desired paramters.  This
        allows for multiprocessing where only the fit statistic gets passed between nodes.
        """
        logging.info('Calculating params:')
        for parname, parval, newparval in zip(self.model.parnames, self.model.parvals, parvals):
            if parval != newparval:
                fit_logger.info('  {0}: {1}'.format(parname, newparval))
        self.model.parvals = parvals

        return np.ones_like(x)


class CalcStat(object):
    def __init__(self, model, pipe):
        self.pipe = pipe
        self.model = model
        self.cache_fit_stat = {}
        self.min_fit_stat = None
        self.min_par_vals = self.model.parvals

    def __call__(self, _data, _model, staterror=None, syserror=None, weight=None):
        """Calculate fit statistic for the xija model.  The args _data and _model
        are sent by Sherpa but they are fictitious -- the real data and model are
        stored in the xija model self.model.
        """
        try:
            raise KeyError
            # fit_stat = self.cache_fit_stat[parvals_key]
            # fit_logger.info('nmass_model: Cache hit %s' % str(parvals_key))
        except KeyError:
            fit_stat = self.model.calc_stat()

        fit_logger.info('Fit statistic: %.4f' % fit_stat)
        # self.cache_fit_stat[parvals_key] = fit_stat

        if self.min_fit_stat is None or fit_stat < self.min_fit_stat:
            self.min_fit_stat = fit_stat
            self.min_parvals = self.model.parvals

        self.message = {'status': 'fitting',
                        'time': time.time(),
                        'parvals': self.model.parvals,
                        'fit_stat': fit_stat,
                        'min_parvals': self.min_parvals,
                        'min_fit_stat': self.min_fit_stat}
        self.pipe.send(self.message)

        while self.pipe.poll():
            pipe_val = self.pipe.recv()
            if pipe_val == 'terminate':
                self.model.parvals = self.min_parvals
                raise FitTerminated('terminated')

        return fit_stat, np.ones(1)


class FitWorker(object):
    def __init__(self, model, method='simplex'):
        self.model = model
        self.method = method
        self.parent_pipe, self.child_pipe = mp.Pipe()

    def start(self, widget=None):
        """Start a Sherpa fit process as a spawned (non-blocking) process.
        """
        self.fit_process = mp.Process(target=self.fit)
        self.fit_process.start()
        logging.info('Fit started')

    def terminate(self, widget=None):
        """Terminate a Sherpa fit process in a controlled way by sending a
        message.  Get the final parameter values if possible.
        """
        if hasattr(self, "fit_process"):
            # Only do this if we had started a fit to begin with
            self.parent_pipe.send('terminate')
            self.fit_process.join()
            logging.info('Fit terminated')

    def fit(self):
        dummy_data = np.zeros(1)
        dummy_times = np.arange(1)
        ui.load_arrays(1, dummy_times, dummy_data)
        ui.set_method(self.method)
        ui.get_method().config.update(sherpa_configs.get(self.method, {}))
        ui.load_user_model(CalcModel(self.model), 'xijamod')  # sets global xijamod
        ui.add_user_pars('xijamod', self.model.parnames)
        ui.set_model(1, 'xijamod')
        calc_stat = CalcStat(self.model, self.child_pipe)
        ui.load_user_stat('xijastat', calc_stat, lambda x: np.ones_like(x))
        ui.set_stat(xijastat)

        # Set frozen, min, and max attributes for each xijamod parameter
        for par in self.model.pars:
            xijamod_par = getattr(xijamod, par.full_name)
            xijamod_par.val = par.val
            xijamod_par.frozen = par.frozen
            xijamod_par.min = par.min
            xijamod_par.max = par.max

        if any(not par.frozen for par in self.model.pars):
            try:
                ui.fit(1)
                calc_stat.message['status'] = 'finished'
                logging.info('Fit finished normally')
            except FitTerminated as err:
                calc_stat.message['status'] = 'terminated'
                logging.warning('Got FitTerminated exception {}'.format(err))

        self.child_pipe.send(calc_stat.message)
