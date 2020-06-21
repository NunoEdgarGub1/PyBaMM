import jax
import jax.numpy as np
import numpy as onp
import scipy
import pybamm
import time

from jax.config import config
config.update("jax_enable_x64", True)


MAX_ORDER = 5
NEWTON_MAXITER = 4
MIN_FACTOR = 0.2
MAX_FACTOR = 10


def flax_cond(pred, true_operand, true_fun, false_operand, false_fun):
    if pred:
        return true_fun(true_operand)
    else:
        return false_fun(false_operand)


def flax_while_loop(cond_fun, body_fun, init_val):
    val = init_val
    while cond_fun(val):
        val = body_fun(val)
    return val


def flax_fori_loop(start, stop, body_fun, init_val):
    val = init_val
    for i in range(start, stop):
        val = body_fun(i, val)
    return val


def compute_R(order, factor):
    """
    computes the R matrix with entries
    given by the first equation on page 8 of [1]

    This is used to update the differences matrix when step size h is varied according
    to factor = h_{n+1} / h_n

    Note that the U matrix also defined in the same section can be also be
    found using factor = 1, which corresponds to R with a constant step size

    """
    I = np.arange(1, MAX_ORDER + 1).reshape(-1, 1)
    J = np.arange(1, MAX_ORDER + 1)
    M = np.empty((MAX_ORDER + 1, MAX_ORDER + 1))
    M = jax.ops.index_update(M, jax.ops.index[1:, 1:],
                             (I - 1 - factor * J) / I)
    M = jax.ops.index_update(M, jax.ops.index[0], 1)
    R = np.cumprod(M, axis=0)

    return R


def _bdf_init(fun, jac, t0, y0, h0, rtol, atol):
    """
    Backward Difference formula (BDF) implicit multistep integrator. The basic algorithm
    is derived in [2]. This particular implementation follows that implemented in the
    Matlab routine ode15s, described in [1], which features the NDF formulas for
    improved stability, with associated differences in the error constants, and
    calculates the jacobian at J(t_n, y_n), rather than at the more standard J(t_{n+1},
    y^0_{n+1}). This implementation was based on that implemented in the scipy library
    [3], which also mainly follows [1] but uses the more standard jacobian update.

    Parameters
    ----------

    fun: callable
        function with signature (t, y), where t is a scalar time and y is a ndarray with
        shape (n,), returns the rhs of the system of ODE equations as an nd array with
        shape (n,)
    jac: callable
        function with signature (t, y), where t is a scalar time and y is a ndarray with
        shape (n,), returns the jacobian matrix of fun as an ndarray with shape (n,n)
    t0: float
        initial time
    y0: ndarray
        initial state vector with shape (n,)


    References
    ----------
    .. [1] L. F. Shampine, M. W. Reichelt, "THE MATLAB ODE SUITE", SIAM J. SCI.
           COMPUTE., Vol. 18, No. 1, pp. 1-22, January 1997.
    .. [2] G. D. Byrne, A. C. Hindmarsh, "A Polyalgorithm for the Numerical
           Solution of Ordinary Differential Equations", ACM Transactions on
           Mathematical Software, Vol. 1, No. 1, pp. 71-96, March 1975.
    .. [3] Virtanen, P., Gommers, R., Oliphant, T. E., Haberland, M., Reddy,
           T., Cournapeau, D., ... & van der Walt, S. J. (2020). SciPy 1.0:
           fundamental algorithms for scientific computing in Python.
           Nature methods, 17(3), 261-272.
    .. [4] E. Hairer, S. P. Norsett G. Wanner, "Solving Ordinary Differential
               Equations I: Nonstiff Problems", Sec. II.4.
    """
    state = {}
    state['t'] = t0
    state['y'] = y0
    #state['fun'] = fun
    f0 = fun(t0, y0)
    state['atol'] = atol
    state['rtol'] = rtol
    order = 1
    state['order'] = order
    state['h'] = _select_initial_step(state, fun, t0, y0, f0, h0)
    EPS = np.finfo(y0.dtype).eps
    state['newton_tol'] = np.max((10 * EPS / rtol, np.min((0.03, rtol ** 0.5))))
    state['n_equal_steps'] = 0
    #state['jac'] = jac
    D = np.empty((MAX_ORDER + 1, len(y0)), dtype=y0.dtype)
    D = jax.ops.index_update(D, jax.ops.index[0, :], y0)
    D = jax.ops.index_update(D, jax.ops.index[1, :], f0 * h0)
    state['D'] = D
    state['y0'] = None
    state['scale_y0'] = None
    state = _predict(state)
    I = np.identity(len(y0), dtype=y0.dtype)
    state['I'] = I

    # kappa values for difference orders, taken from Table 1 of [1]
    kappa = np.array([0, -0.1850, -1 / 9, -0.0823, -0.0415, 0])
    gamma = np.hstack((0, np.cumsum(1 / np.arange(1, MAX_ORDER + 1))))
    alpha = 1.0 / ((1 - kappa) * gamma)
    c = h0 * alpha[order]
    error_const = kappa * gamma + 1 / np.arange(1, MAX_ORDER + 2)

    state['kappa'] = kappa
    state['gamma'] = gamma
    state['alpha'] = alpha
    state['c'] = c
    state['error_const'] = error_const

    J = jac(t0, y0)
    state['J'] = J
    state['LU'] = jax.scipy.linalg.lu_factor(I - c * J)

    state['U'] = [compute_R(order, 1) for order in range(MAX_ORDER)]
    state['psi'] = None
    state = _update_psi(state)
    return state


def _select_initial_step(state, fun, t0, y0, f0, h0):
    """
    Select a good initial step by stepping forward one step of forward euler, and
    comparing the predicted state against that using the provided function.

    Optimal step size based on the selected order is obtained using formula (4.12)
    in [4]
    """
    scale = state['atol'] + np.abs(y0) * state['rtol']
    y1 = y0 + h0 * f0
    f1 = fun(t0 + h0, y1)
    d2 = np.sqrt(np.mean(((f1 - f0) / scale)))
    order = 1
    h1 = h0 * d2 ** (-1 / (order + 1))
    return np.min((100 * h0, h1))


def _predict(state):
    """
    predict forward to new step (eq 2 in [1])
    """
    n = len(state['y'])
    order = state['order']
    orders = np.repeat(np.arange(MAX_ORDER + 1).reshape(-1, 1), n, axis=1)
    subD = np.where(orders <= order, state['D'], 0)
    state['y0'] = np.sum(subD, axis=0)
    state['scale_y0'] = state['atol'] + state['rtol'] * np.abs(state['y0'])
    return state


def _update_psi(state):
    """
    update psi term as defined in second equation on page 9 of [1]
    """
    order = state['order']
    n = len(state['y'])
    orders = np.arange(MAX_ORDER + 1)
    subGamma = np.where(orders > 0, np.where(orders <= order, state['gamma'], 0), 0)
    orders = np.repeat(orders.reshape(-1, 1), n, axis=1)
    subD = np.where(orders > 0, np.where(orders <= order, state['D'], 0), 0)
    state['psi'] = np.dot(
        subD.T,
        subGamma
    ) * state['alpha'][order]
    return state


def _update_difference_for_next_step(state, d, only_update_D=False):
    """
    update of difference equations can be done efficiently
    by reusing d and D.

    From first equation on page 4 of [1]:
    d = y_n - y^0_n = D^{k + 1} y_n

    Standard backwards difference gives
    D^{j + 1} y_n = D^{j} y_n - D^{j} y_{n - 1}

    Combining these gives the following algorithm
    """
    order = state['order']
    D = state['D']
    D = jax.ops.index_update(D, jax.ops.index[order + 2],
                             d - D[order + 1])
    D = jax.ops.index_update(D, jax.ops.index[order + 1],
                             d)
    i = order
    while_state = [i, D]

    def while_cond(while_state):
        i, D = while_state
        return i >= 0

    def while_body(while_state):
        i, D = while_state
        D = jax.ops.index_add(D, jax.ops.index[i],
                              D[i + 1])
        i -= 1
        return [i, D]

    i, D = jax.lax.while_loop(while_cond, while_body, while_state)

    state['D'] = D

    def update_psi_and_predict(state):
        # update psi (D has changed)
        state = _update_psi(state)

        # update y0 (D has changed)
        state = _predict(state)

        return state

    state = jax.lax.cond(only_update_D == False,
                      state, update_psi_and_predict,
                      state, lambda x: x)

    return state


def _update_step_size(state, factor, dont_update_lu):
    """
    If step size h is changed then also need to update the terms in
    the first equation of page 9 of [1]:

    - constant c = h / (1-kappa) gamma_k term
    - lu factorisation of (I - c * J) used in newton iteration (same equation)
    - psi term
    """
    order = state['order']
    h = state['h']

    h *= factor
    state['n_equal_steps'] = 0
    c = h * state['alpha'][order]

    # redo lu (c has changed)
    def update_lu(state):
        state['LU'] = jax.scipy.linalg.lu_factor(state['I'] - c * state['J'])
        return state

    state = jax.lax.cond(dont_update_lu == False,
                      state, update_lu,
                      state, lambda x: x)

    state['h'] = h
    state['c'] = c

    # update D using equations in section 3.2 of [1]
    RU = compute_R(order, factor).dot(state['U'][0])
    I = np.arange(0, MAX_ORDER + 1).reshape(-1, 1)
    J = np.arange(0, MAX_ORDER + 1)

    # only update order+1, order+1 entries of D
    RU = np.where(np.logical_and(I <= order, J <= order),
                  RU, np.identity(MAX_ORDER + 1))
    D = state['D']
    D = np.dot(RU.T, D)
    # D = jax.ops.index_update(D, jax.ops.index[:order + 1],
    #                         np.dot(RU.T, D[:order + 1]))
    state['D'] = D

    # update psi (D has changed)
    state = _update_psi(state)

    # update y0 (D has changed)
    state = _predict(state)

    return state


def _update_jacobian(state, jac):
    """
    we update the jacobian using J(t_n, y_n) as per [1]

    Note: this is slightly different than the standard practice
    of using J(t_{n+1}, y^0_{n+1})
    """
    J = jac(state['t'], state['y'])
    state['LU'] = jax.scipy.linalg.lu_factor(state['I'] - state['c'] * J)
    state['J'] = J
    return state


def _newton_iteration(state, fun):
    tol = state['newton_tol']
    c = state['c']
    psi = state['psi']
    y0 = state['y0']
    LU = state['LU']
    scale_y0 = state['scale_y0']
    t = state['t'] + state['h']
    d = np.zeros_like(y0)
    y = np.array(y0, copy=True)

    not_converged = True
    dy_norm_old = -1.0
    k = 0
    while_state = [k, not_converged, dy_norm_old, d, y]

    def while_cond(state):
        k, not_converged, _, _, _ = state
        return not_converged * (k < NEWTON_MAXITER)

    def while_body(state):
        k, not_converged, dy_norm_old, d, y = state
        f_eval = fun(t, y)
        b = c * f_eval - psi - d
        dy = jax.scipy.linalg.lu_solve(LU, b)
        dy_norm = np.sqrt(np.mean((dy / scale_y0)**2))
        rate = dy_norm / dy_norm_old

        # if iteration is not going to converge in NEWTON_MAXITER
        # (assuming the current rate), then abort
        pred = rate ** (NEWTON_MAXITER - k) / (1 - rate) * dy_norm
        pred = rate >= 1
        pred += rate ** (NEWTON_MAXITER - k) / (1 - rate) * dy_norm > tol
        pred *= dy_norm_old < -1
        k = jax.lax.cond(pred, k, lambda k: NEWTON_MAXITER, k, lambda k: k)

        d += dy
        y = y0 + d

        # if converged then break out of iteration early
        pred = rate > 0.0
        pred *= rate / (1 - rate) * dy_norm < tol
        pred += dy_norm == 0

        def converged_fun(state):
            k, not_converged, dy_norm_old = state
            not_converged = False
            return [k, not_converged, dy_norm_old]

        def not_converged_fun(state):
            k, not_converged, dy_norm_old = state
            dy_norm_old = dy_norm
            k += 1
            return [k, not_converged, dy_norm_old]

        if_state = [k, not_converged, dy_norm_old]
        k, not_converged, dy_norm_old = \
            jax.lax.cond(pred, if_state, converged_fun, if_state, not_converged_fun)
        return [k, not_converged, dy_norm_old, d, y]

    k, not_converged, dy_norm_old, d, y = jax.lax.while_loop(while_cond, while_body,
                                                          while_state)
    return not_converged, k + 1, y, d


def _bdf_step(state, fun, jac):
    # we will try and use the old jacobian unless convergence of newton iteration
    # fails
    not_updated_jacobian = True
    # initialise step size and try to make the step,
    # iterate, reducing step size until error is in bounds
    step_accepted = False
    y = np.empty_like(state['y'])
    d = np.empty_like(state['y'])
    n_iter = -1
    while_state = [state, step_accepted, not_updated_jacobian, y, d, n_iter]

    def while_cond(while_state):
        _, step_accepted, _, _, _, _ = while_state
        return step_accepted == False

    def while_body(while_state):
        state, step_accepted, not_updated_jacobian, y, d, n_iter = while_state

        # solve BDF equation using y0 as starting point
        not_converged, n_iter, y, d = _newton_iteration(state, fun)

        # if not converged update the jacobian for J(t_n,y_n) and try again
        pred = not_converged * not_updated_jacobian
        if_state = [state, not_updated_jacobian, step_accepted]

        def need_to_update_jacobian(if_state):
            state, not_updated_jacobian, step_accepted = if_state
            state = _update_jacobian(state, jac)
            not_updated_jacobian = False
            return [state, not_updated_jacobian, step_accepted]

        def dont_need_to_update_jacobian(if_state):
            state, not_updated_jacobian, step_accepted = if_state

            if_state2 = [state, step_accepted]
            # if still not converged then multiply step size by 0.3 (as per [1])
            # and try again

            def need_to_update_step_size(if_state2):
                state, step_accepted = if_state2
                state = _update_step_size(state, 0.3, False)
                return [state, step_accepted]

            def converged(if_state2):
                state, step_accepted = if_state2
                # yay, converged, now check error is within bounds
                scale_y = state['atol'] + state['rtol'] * np.abs(y)

                # combine eq 3, 4 and 6 from [1] to obtain error
                # Note that error = C_k * h^{k+1} y^{k+1}
                # and d = D^{k+1} y_{n+1} \approx h^{k+1} y^{k+1}
                error = state['error_const'][state['order']] * d
                error_norm = np.sqrt(np.mean((error / scale_y)**2))

                # calculate safety outside if since we will reuse later
                safety = 0.9 * (2 * NEWTON_MAXITER + 1) / (2 * NEWTON_MAXITER
                                                           + n_iter)

                if_state3 = [state, step_accepted]
                # if error too large, reduce step size and try again

                def error_too_large(if_state3):
                    state, step_accepted = if_state3
                    # calculate optimal step size factor as per eq 2.46 of [2]
                    factor = np.max((MIN_FACTOR,
                                     safety * error_norm ** (-1 / (state['order'] + 1))))
                    state = _update_step_size(state, factor, False)
                    return [state, step_accepted]

                # if we get here we can accept the step
                def step_accepted(if_state3):
                    state, step_accepted = if_state3
                    step_accepted = True
                    return [state, step_accepted]

                state, step_accepted = \
                    jax.lax.cond(error_norm > 1,
                              if_state3, error_too_large,
                              if_state3, step_accepted)

                return [state, step_accepted]

            state, step_accepted = jax.lax.cond(not_converged,
                                             if_state2, need_to_update_step_size,
                                             if_state2, converged)

            return [state, not_updated_jacobian, step_accepted]

        state, not_updated_jacobian, step_accepted = \
            jax.lax.cond(pred,
                      if_state, need_to_update_jacobian,
                      if_state, dont_need_to_update_jacobian)
        return [state, step_accepted, not_updated_jacobian, y, d, n_iter]

    state, step_accepted, not_updated_jacobian, y, d, n_iter = \
        jax.lax.while_loop(while_cond, while_body, while_state)

    # take the accepted step
    state['y'] = y
    state['t'] += state['h']

    # a change in order is only done after running at order k for k + 1 steps
    # (see page 83 of [2])
    state['n_equal_steps'] += 1

    if_state = [state, d, y, n_iter]

    def no_change_in_order(if_state):
        state, d, _, _ = if_state
        state = _update_difference_for_next_step(state, d, False)
        return state

    def order_change(if_state):
        state, d, y, n_iter = if_state
        order = state['order']

        # Note: we are recalculating these from the while loop above, could re-use?
        scale_y = state['atol'] + state['rtol'] * np.abs(y)
        error = state['error_const'][order] * d
        error_norm = np.sqrt(np.mean((error / scale_y)**2))
        safety = 0.9 * (2 * NEWTON_MAXITER + 1) / (2 * NEWTON_MAXITER
                                                   + n_iter)

        # don't need to update psi and y0 yet as we will be changing D again soon
        state = _update_difference_for_next_step(state, d, True)

        if_state2 = [state, scale_y, order]
        # similar to the optimal step size factor we calculated above for the current
        # order k, we need to calculate the optimal step size factors for orders
        # k-1 and k+1. To do this, we note that the error = C_k * D^{k+1} y_n

        def order_greater_one(if_state2):
            state, scale_y, order = if_state2
            error_m = state['error_const'][order - 1] * state['D'][order]
            error_m_norm = np.sqrt(np.mean((error_m / scale_y)**2))
            return error_m_norm

        def order_equal_one(if_state2):
            error_m_norm = np.inf
            return error_m_norm

        error_m_norm = jax.lax.cond(order > 1,
                                 if_state2, order_greater_one,
                                 if_state2, order_equal_one)

        def order_less_max(if_state2):
            state, scale_y, order = if_state2
            error_p = state['error_const'][order + 1] * state['D'][order + 2]
            error_p_norm = np.sqrt(np.mean((error_p / scale_y)**2))
            return error_p_norm

        def order_max(if_state2):
            error_p_norm = np.inf
            return error_p_norm

        error_p_norm = jax.lax.cond(order < MAX_ORDER,
                                 if_state2, order_less_max,
                                 if_state2, order_max)

        error_norms = np.array([error_m_norm, error_norm, error_p_norm])
        # with np.errstate(divide='ignore'):
        factors = error_norms ** (-1 / (np.arange(3) + order))

        # now we have the three factors for orders k-1, k and k+1, pick the maximum in
        # order to maximise the resultant step size
        max_index = np.argmax(factors)
        order += max_index - 1
        state['order'] = order

        factor = np.min((MAX_FACTOR, safety * factors[max_index]))
        state = _update_step_size(state, factor, False)

        return state

    state = jax.lax.cond(state['n_equal_steps'] < state['order'] + 1,
                      if_state, no_change_in_order,
                      if_state, order_change)

    return state


def _bdf_interpolate(state, t_eval):
    """
    interpolate solution at time values t* where t-h < t* < t

    definition of the interpolating polynomial can be found on page 7 of [1]
    """
    order = state['order']
    t = state['t']
    h = state['h']
    D = state['D']
    j = 0
    time_factor = 1.0
    order_summation = D[0]
    while_state = [j, time_factor, order_summation]

    def while_cond(while_state):
        j, time_factor, order_summation = while_state
        return j < order

    def while_body(while_state):
        j, time_factor, order_summation = while_state
        time_factor *= (t_eval - (t - h*j)) / (h * (1 + j))
        order_summation += D[j+1] * time_factor
        j += 1
        return [j, time_factor, order_summation]

    j, time_factor, order_summation = jax.lax.while_loop(while_cond,
                                                      while_body,
                                                      while_state)
    return order_summation


@jax.partial(jax.jit, static_argnums=(0, 3, 4))
def _bdf_odeint(fun, y0, t_eval, rtol, atol):

    jac = jax.jacfwd(fun, argnums=1)
    t0 = t_eval[0]
    h0 = t_eval[1] - t0

    stepper = _bdf_init(fun, jac, t0, y0, h0, rtol, atol)
    i = 0
    y_out = np.empty((len(y0), len(t_eval)), dtype=y0.dtype)

    init_state = [stepper, t_eval, i, y_out]

    def cond_fun(state):
        _, t_eval, i, _ = state
        return i < len(t_eval)

    def body_fun(state):
        stepper, t_eval, i, y_out = state
        stepper = _bdf_step(stepper, fun, jac)
        index = np.searchsorted(t_eval, stepper['t'])

        for_state = [y_out, i]

        def for_body(j, for_state):
            y_out, i = for_state
            t = t_eval[j]
            y_out = jax.ops.index_update(y_out, jax.ops.index[:, j],
                                         _bdf_interpolate(stepper, t))
            i += 1
            return [y_out, i]

        y_out, i = jax.lax.fori_loop(i, index, for_body, for_state)
        return [stepper, t_eval, i, y_out]

    stepper, t_eval, i, y_out = jax.lax.while_loop(cond_fun, body_fun, init_state)

    return y_out


def jax_bdf_integrate(fun, y0, t_eval, rtol=1e-6, atol=1e-6):
    y0_device = jax.device_put(y0).reshape(-1)
    t_eval_device = jax.device_put(t_eval)
    y_out = _bdf_odeint(fun, y0_device, t_eval_device, rtol, atol)
    return np.array(y_out)
