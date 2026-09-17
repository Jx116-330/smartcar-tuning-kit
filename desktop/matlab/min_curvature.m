function opt = min_curvature(orig_path_json, opts)
%MIN_CURVATURE  MATLAB-side minimum-curvature path optimizer.
%
%   opt = MIN_CURVATURE(orig_path_json) reads a path JSON produced by
%   path_optimizer.py (either an original recording loaded via
%   Path.from_recording_json or a previously-optimized output), runs
%   the same quadratic program as path_optimizer.min_curvature, and
%   writes the optimized path back to JSON next to the input.
%
%   This is the MATLAB mirror of the Python implementation: same cost
%   function, same constraints, same feasibility guard. Use whichever
%   side has Optimization Toolbox / a faster QP solver available.
%
%   Cost function:
%       J(d) = || A * (P0_x + d.*nx) ||^2 + || A * (P0_y + d.*ny) ||^2
%              + w * || D1 * d ||^2
%   where:
%       A    - N x N second-difference operator
%       D1   - (N-1) x N first-difference operator
%       n    - unit normal at each waypoint
%       d    - lateral offset along normal (decision variable)
%
%   Bounds:
%       d(1) = d(N) = 0          (start/end pinned)
%       |d(i)| <= max_offset_m   (corridor budget)
%
%   opts (all optional, struct):
%       opts.max_offset_m        - lateral budget per point (default 0.30)
%       opts.smoothness_weight   - L2 penalty on first-diff of d (default 0)
%       opts.enforce_feasibility - if true (default), bisect-scale d so
%                                  max|kappa_opt| <= max|kappa_orig| *
%                                  feasibility_margin
%       opts.feasibility_margin  - default 1.05
%       opts.output_json         - output path (default: input with
%                                  "_matlab_mincurv" suffix)
%       opts.verbose             - default true
%
%   REAL-CAR SAFETY NOTE
%   --------------------
%   This is a pure geometry optimizer. It does NOT use vehicle_params
%   (specifically NOT the USER_SIM WHEELBASE_M). Feasibility is
%   enforced against the observed minimum radius of the input path,
%   not a kinematic limit derived from unverified geometry. So the
%   output is safe to drive iff the input recording was drivable.
%
%   Example:
%       opt = min_curvature('E:/ads/tcp_tool/paths/curve175_orig.json', ...
%               struct('max_offset_m', 0.5, 'enforce_feasibility', true));

    arguments
        orig_path_json (1,:) char
        opts.max_offset_m        (1,1) double = 0.30
        opts.smoothness_weight   (1,1) double = 0.0
        opts.enforce_feasibility (1,1) logical = true
        opts.feasibility_margin  (1,1) double = 1.05
        opts.output_json         (1,:) char = ''
        opts.verbose             (1,1) logical = true
    end

    % --- Load input ----------------------------------------------------
    raw = jsondecode(fileread(orig_path_json));
    if isfield(raw, 'points')
        pts = raw.points;
    else
        error('min_curvature:badformat', ...
              'expected {meta, points} JSON; got %s', class(raw));
    end
    % pts may be a struct array or a cell array depending on field
    % uniformity. Normalize to N x 3.
    if iscell(pts)
        N = numel(pts);
        x = zeros(N, 1); y = zeros(N, 1); yaw = zeros(N, 1);
        for i = 1:N
            x(i)   = pts{i}.x_m;
            y(i)   = pts{i}.y_m;
            yaw(i) = pts{i}.yaw_deg;
        end
    else
        N = numel(pts);
        x = [pts.x_m]'; y = [pts.y_m]'; yaw = [pts.yaw_deg]';
    end

    if opts.verbose
        fprintf('[min_curvature] %s: %d points\n', orig_path_json, N);
    end
    if N < 5
        opt = struct('x', x, 'y', y, 'yaw_deg', yaw);
        warning('min_curvature:tooshort', 'fewer than 5 points; skipped');
        return
    end

    % --- Geometry: tangents and normals --------------------------------
    [nx, ny] = compute_normals(x, y);

    % --- Build QP matrices --------------------------------------------
    A  = second_diff_matrix(N);
    A_nx = A .* nx';         % broadcast: row i col j -> A(i,j)*nx(j)
    A_ny = A .* ny';
    c_x  = A * x;
    c_y  = A * y;

    H = 2 * (A_nx' * A_nx + A_ny' * A_ny);
    f = 2 * (A_nx' * c_x  + A_ny' * c_y);

    if opts.smoothness_weight > 0
        D1 = first_diff_matrix(N);
        H = H + 2 * opts.smoothness_weight * (D1' * D1);
    end

    % --- Bounds: start/end fixed at 0, interior in +/- max_offset ----
    lb = -opts.max_offset_m * ones(N, 1);
    ub =  opts.max_offset_m * ones(N, 1);
    lb(1) = 0; lb(end) = 0;
    ub(1) = 0; ub(end) = 0;

    % Symmetrize H for quadprog (must be exactly symmetric).
    H = 0.5 * (H + H');

    % --- Solve ---------------------------------------------------------
    if exist('quadprog', 'file') == 2
        qopts = optimoptions('quadprog', 'Display', 'off', ...
                             'Algorithm', 'interior-point-convex');
        tic;
        [d, fval, exitflag] = quadprog(H, f, [], [], [], [], lb, ub, ...
                                        zeros(N, 1), qopts);
        elapsed = toc;
        converged = (exitflag == 1);
        if isempty(d)
            error('min_curvature:qpfail', ...
                  'quadprog failed (exitflag=%d)', exitflag);
        end
    else
        % Optimization Toolbox missing; fall back to a projected
        % gradient descent. Slower but doesn't require a license.
        warning('min_curvature:noquadprog', ...
                ['quadprog not found; using projected gradient. ', ...
                 'Install Optimization Toolbox for faster solve.']);
        d = zeros(N, 1);
        step = 1.0 / max(eig(H));
        tic;
        for it = 1:2000
            g = H * d + f;
            d_new = d - step * g;
            d_new = max(lb, min(ub, d_new));
            if max(abs(d_new - d)) < 1e-6
                d = d_new; break
            end
            d = d_new;
        end
        elapsed = toc;
        fval = 0.5 * d' * H * d + f' * d;
        converged = true;
    end

    % --- Feasibility scaling (bisection, mirror of Python side) -------
    orig_kmax = max_abs_curvature(x, y);
    feasibility_action = 'none';
    scale = 1.0;
    if opts.enforce_feasibility && orig_kmax > 1e-9
        target_kmax = orig_kmax * opts.feasibility_margin;
        cand_x = x + d .* nx;
        cand_y = y + d .* ny;
        if max_abs_curvature(cand_x, cand_y) > target_kmax
            lo = 0; hi = 1;
            for it = 1:40
                mid = 0.5 * (lo + hi);
                trial_x = x + mid * d .* nx;
                trial_y = y + mid * d .* ny;
                if max_abs_curvature(trial_x, trial_y) <= target_kmax
                    lo = mid;
                else
                    hi = mid;
                end
            end
            scale = lo;
            d = scale * d;
            feasibility_action = sprintf('scaled to %.3f', scale);
        end
    end

    x_new = x + d .* nx;
    y_new = y + d .* ny;
    yaw_new = derive_yaw(x_new, y_new);

    % --- Build report --------------------------------------------------
    orig_len = arc_length(x, y);
    opt_len  = arc_length(x_new, y_new);
    opt_kmax = max_abs_curvature(x_new, y_new);
    max_offset_used = max(abs(d));

    if opts.verbose
        fprintf('[min_curvature] arc: %.2fm -> %.2fm  (%.1f%%)\n', ...
                orig_len, opt_len, 100*(opt_len-orig_len)/orig_len);
        fprintf('[min_curvature] max|k|: %.3f -> %.3f 1/m  (%.1f%%)\n', ...
                orig_kmax, opt_kmax, 100*(opt_kmax-orig_kmax)/orig_kmax);
        fprintf('[min_curvature] max lateral offset: %.3fm (budget %.2fm)\n', ...
                max_offset_used, opts.max_offset_m);
        fprintf('[min_curvature] feasibility: %s\n', feasibility_action);
        fprintf('[min_curvature] solver elapsed: %.3fs\n', elapsed);
    end

    % --- Write output --------------------------------------------------
    if isempty(opts.output_json)
        [d_, base, ~] = fileparts(orig_path_json);
        opts.output_json = fullfile(d_, [base, '_matlab_mincurv.json']);
    end

    meta = struct();
    if isfield(raw, 'meta')
        meta = raw.meta;
    end
    meta.optimization = struct( ...
        'method',              'min_curvature_matlab', ...
        'max_offset_m',        opts.max_offset_m, ...
        'smoothness_weight',   opts.smoothness_weight, ...
        'enforce_feasibility', opts.enforce_feasibility, ...
        'feasibility_margin',  opts.feasibility_margin, ...
        'feasibility_action',  feasibility_action, ...
        'feasibility_scale',   scale, ...
        'elapsed_s',           elapsed, ...
        'converged',           converged, ...
        'cost_final',          fval, ...
        'max_offset_used_m',   max_offset_used);

    out_struct = struct('meta', meta, 'points', ...
        struct('x_m', num2cell(x_new), ...
               'y_m', num2cell(y_new), ...
               'yaw_deg', num2cell(yaw_new)));
    fid = fopen(opts.output_json, 'w');
    fprintf(fid, '%s', jsonencode(out_struct, 'PrettyPrint', true));
    fclose(fid);
    if opts.verbose
        fprintf('[min_curvature] saved %s\n', opts.output_json);
    end

    opt = struct('x', x_new, 'y', y_new, 'yaw_deg', yaw_new, ...
                 'd', d, 'meta', meta, 'output_json', opts.output_json);
end


function [nx, ny] = compute_normals(x, y)
    n = numel(x);
    tx = zeros(n, 1); ty = zeros(n, 1);
    tx(2:end-1) = x(3:end) - x(1:end-2);
    ty(2:end-1) = y(3:end) - y(1:end-2);
    tx(1)   = x(2)   - x(1);     ty(1)   = y(2)   - y(1);
    tx(end) = x(end) - x(end-1); ty(end) = y(end) - y(end-1);
    mag = hypot(tx, ty); mag(mag < 1e-9) = 1;
    tx = tx ./ mag; ty = ty ./ mag;
    nx = -ty;  ny = tx;
end


function A = second_diff_matrix(n)
    A = zeros(n);
    for i = 2:n-1
        A(i, i-1) =  1;
        A(i, i)   = -2;
        A(i, i+1) =  1;
    end
end


function D = first_diff_matrix(n)
    D = zeros(n-1, n);
    for i = 1:n-1
        D(i, i)   = -1;
        D(i, i+1) =  1;
    end
end


function k = max_abs_curvature(x, y)
    n = numel(x);
    k = 0;
    if n < 3, return; end
    for i = 2:n-1
        dx1 = x(i) - x(i-1); dy1 = y(i) - y(i-1);
        dx2 = x(i+1) - x(i); dy2 = y(i+1) - y(i);
        cross = dx1*dy2 - dy1*dx2;
        L1 = hypot(dx1, dy1);
        L2 = hypot(dx2, dy2);
        L3 = hypot(x(i+1) - x(i-1), y(i+1) - y(i-1));
        denom = L1 * L2 * L3;
        if denom > 1e-9
            ki = abs(2 * cross / denom);
            if ki > k, k = ki; end
        end
    end
end


function L = arc_length(x, y)
    L = sum(hypot(diff(x), diff(y)));
end


function yaw = derive_yaw(x, y)
    n = numel(x);
    dx = zeros(n, 1); dy = zeros(n, 1);
    dx(2:end-1) = (x(3:end) - x(1:end-2)) / 2;
    dy(2:end-1) = (y(3:end) - y(1:end-2)) / 2;
    dx(1)   = x(2)   - x(1);     dy(1)   = y(2)   - y(1);
    dx(end) = x(end) - x(end-1); dy(end) = y(end) - y(end-1);
    yaw = atan2d(dy, dx);
end
