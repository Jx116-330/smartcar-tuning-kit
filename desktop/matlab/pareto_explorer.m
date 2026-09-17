function out = pareto_explorer(master_csv, opts)
%PARETO_EXPLORER  3D Pareto visualization of a param_sweep result.
%
%   out = PARETO_EXPLORER(master_csv) reads a master.csv produced by
%   param_sweep.py and shows interactive Pareto-front plots:
%       Panel 1: 3D scatter (XTE rms, duration, steer_dstr_std)
%                colored by KP, marker shape per HEAD_BLEND
%       Panel 2: 2D XTE vs duration projection
%       Panel 3: 2D XTE vs steer-jitter projection
%       Panel 4: Pareto subset only, sortable by any axis
%
%   out = PARETO_EXPLORER(master_csv, opts) takes options:
%       opts.path_name    - filter to one path_name from the master
%                           (default: first unique value)
%       opts.pareto_csv   - read pareto subset from a separate CSV file
%                           (default: compute Pareto in MATLAB)
%       opts.save_path    - if set, saveas(fig, opts.save_path)
%
%   *** SIMULATION TRUST GUARD ***
%   The CSV being visualized comes from param_sweep.py, which runs
%   sim_pure_pursuit.simulate(). That simulator consumes
%   vehicle_params.WHEELBASE_M, currently tagged USER_SIM (the value
%   came from the user's earlier Ackermann sim script and was not
%   physically measured). If WHEELBASE_M is wrong, the entire
%   kinematic-bicycle yaw response is scaled wrong, so the Pareto
%   ordering shown here is informative for COMPARING parameter
%   combinations RELATIVE TO EACH OTHER, but NOT for setting on-car
%   parameters in absolute terms. The script prints a banner reminding
%   you of this every time it loads a CSV.
%
%   Example:
%       pareto_explorer('E:/ads/tcp_tool/sweeps/2026-05-12-run01/master.csv');

    arguments
        master_csv (1,:) char
        opts.path_name   (1,:) char = ''
        opts.pareto_csv  (1,:) char = ''
        opts.save_path   (1,:) char = ''
    end

    if ~isfile(master_csv)
        error('pareto_explorer:nofile', 'master.csv not found: %s', master_csv);
    end

    print_trust_banner(master_csv);

    T = readtable(master_csv);

    paths = unique(string(T.path_name));
    if isempty(opts.path_name)
        opts.path_name = char(paths(1));
    end
    if ~any(paths == opts.path_name)
        error('pareto_explorer:badpath', ...
              'path_name "%s" not in master.csv (have: %s)', ...
              opts.path_name, strjoin(paths, ', '));
    end
    T = T(strcmp(T.path_name, opts.path_name), :);

    % Drop runs that hit lost-path or that have NaN in any axis. They
    % distort the visual range and are never on the Pareto front anyway.
    %
    % param_sweep.py writes the lost column as Python repr "True"/"False",
    % which readtable surfaces as either string/cellstr (older readtable)
    % or as logical (newer); coerce both into a logical mask.
    lost_logical = coerce_logical(T.lost);
    keep = ~lost_logical ...
         & ~isnan(T.xte_rms_m) ...
         & ~isnan(T.duration_s) ...
         & ~isnan(T.steer_dstr_std_deg);
    T = T(keep, :);
    fprintf('pareto_explorer: %d valid combos for path=%s\n', ...
            height(T), opts.path_name);

    % Determine the Pareto subset. Either load from a separate file or
    % compute in MATLAB - both should produce the same set if Python
    % and MATLAB stay in sync.
    if ~isempty(opts.pareto_csv) && isfile(opts.pareto_csv)
        P_table = readtable(opts.pareto_csv);
        P_table = P_table(strcmp(P_table.path_name, opts.path_name), :);
        on_front = ismember(T.kp, P_table.kp) & ismember(T.la, P_table.la) ...
                 & ismember(T.cap, P_table.cap) & ismember(T.blend, P_table.blend);
    else
        on_front = compute_pareto_4d(T);
    end

    % --- Figure ------------------------------------------------------
    fig = figure('Name', sprintf('Pareto Explorer - %s', opts.path_name), ...
                 'Position', [60, 60, 1500, 900], ...
                 'Color', [0.12 0.14 0.18]);

    % Panel 1: 3D scatter.
    ax1 = subplot(2, 2, 1, 'Parent', fig);
    hold(ax1, 'on'); grid(ax1, 'on');
    scatter3(ax1, T.xte_rms_m * 100, T.duration_s, T.steer_dstr_std_deg, ...
             30, T.kp, 'filled', 'MarkerFaceAlpha', 0.4);
    if any(on_front)
        scatter3(ax1, T.xte_rms_m(on_front) * 100, T.duration_s(on_front), ...
                 T.steer_dstr_std_deg(on_front), 80, [1 0.5 0.0], ...
                 'filled', 'MarkerEdgeColor', 'w', 'LineWidth', 0.8);
    end
    view(ax1, -45, 20);
    xlabel(ax1, 'XTE rms (cm)', 'Color', 'w');
    ylabel(ax1, 'duration (s)',  'Color', 'w');
    zlabel(ax1, 'd-steer std (deg)', 'Color', 'w');
    cb = colorbar(ax1); cb.Color = 'w';
    cb.Label.String = 'KP'; cb.Label.Color = 'w';
    title(ax1, sprintf('3D Pareto scatter (orange = front, n=%d/%d)', ...
          sum(on_front), height(T)), 'Color', 'w');
    style_axes(ax1);

    % Panel 2: 2D xte vs duration.
    ax2 = subplot(2, 2, 2, 'Parent', fig);
    hold(ax2, 'on'); grid(ax2, 'on');
    scatter(ax2, T.xte_rms_m * 100, T.duration_s, 28, T.kp, 'filled', ...
            'MarkerFaceAlpha', 0.5);
    if any(on_front)
        scatter(ax2, T.xte_rms_m(on_front) * 100, T.duration_s(on_front), ...
                70, [1 0.5 0.0], 'filled', 'MarkerEdgeColor', 'w');
    end
    xlabel(ax2, 'XTE rms (cm)', 'Color', 'w');
    ylabel(ax2, 'duration (s)',  'Color', 'w');
    title(ax2, 'XTE vs duration', 'Color', 'w');
    style_axes(ax2);

    % Panel 3: 2D xte vs steer-jitter.
    ax3 = subplot(2, 2, 3, 'Parent', fig);
    hold(ax3, 'on'); grid(ax3, 'on');
    scatter(ax3, T.xte_rms_m * 100, T.steer_dstr_std_deg, 28, T.la, ...
            'filled', 'MarkerFaceAlpha', 0.5);
    if any(on_front)
        scatter(ax3, T.xte_rms_m(on_front) * 100, ...
                T.steer_dstr_std_deg(on_front), 70, [1 0.5 0.0], ...
                'filled', 'MarkerEdgeColor', 'w');
    end
    cb3 = colorbar(ax3); cb3.Color = 'w';
    cb3.Label.String = 'LA (mm)'; cb3.Label.Color = 'w';
    xlabel(ax3, 'XTE rms (cm)', 'Color', 'w');
    ylabel(ax3, 'd-steer std (deg)', 'Color', 'w');
    title(ax3, 'XTE vs steer jitter', 'Color', 'w');
    style_axes(ax3);

    % Panel 4: Pareto-only sortable table view as a heatmap-ish bar plot.
    ax4 = subplot(2, 2, 4, 'Parent', fig);
    hold(ax4, 'on'); grid(ax4, 'on');
    if any(on_front)
        Pf = T(on_front, :);
        Pf = sortrows(Pf, 'xte_rms_m');
        Nf = height(Pf);
        x_axis = 1:Nf;
        plot(ax4, x_axis, Pf.xte_rms_m * 100, '-o', 'Color', [1 0.5 0.0], ...
            'LineWidth', 1.3, 'DisplayName', 'XTE rms (cm)');
        plot(ax4, x_axis, Pf.steer_dstr_std_deg, '-o', 'Color', [0.4 0.8 1.0], ...
            'LineWidth', 1.3, 'DisplayName', 'd-steer std (deg)');
        plot(ax4, x_axis, Pf.sign_flips, '-o', 'Color', [0.95 0.4 0.7], ...
            'LineWidth', 1.3, 'DisplayName', 'sign flips');
        legend(ax4, 'TextColor', 'w', 'Color', [0.2 0.22 0.27], ...
               'Location', 'best');
        xlabel(ax4, 'rank on Pareto front (xte order)', 'Color', 'w');
        title(ax4, 'Pareto front members - metric profile', 'Color', 'w');
    else
        text(ax4, 0.5, 0.5, 'No Pareto candidates', ...
             'Color', 'w', 'HorizontalAlignment', 'center');
    end
    style_axes(ax4);

    sgtitle(fig, sprintf('Pareto Explorer  -  %s', opts.path_name), ...
            'Color', 'w', 'FontSize', 14, 'FontWeight', 'bold');

    if ~isempty(opts.save_path)
        saveas(fig, opts.save_path);
        fprintf('pareto_explorer: saved %s\n', opts.save_path);
    end

    out = struct( ...
        'all',     T, ...
        'pareto',  T(on_front, :), ...
        'fig',     fig);
end


function front = compute_pareto_4d(T)
%COMPUTE_PARETO_4D  Mirror of param_sweep.py:pareto_front (4 objectives).
%
%   Objectives (minimize all): xte_rms_m, duration_s,
%   steer_dstr_std_deg, sign_flips. Rows that hit lost-path or have NaN
%   in any objective are excluded upstream.
    cols = [T.xte_rms_m, T.duration_s, T.steer_dstr_std_deg, ...
            double(T.sign_flips)];
    N = size(cols, 1);
    front = false(N, 1);
    for i = 1:N
        a = cols(i, :);
        dominated = false;
        for j = 1:N
            if i == j, continue; end
            b = cols(j, :);
            if all(b <= a) && any(b < a)
                dominated = true;
                break
            end
        end
        front(i) = ~dominated;
    end
end


function print_trust_banner(master_csv)
    fprintf('\n');
    fprintf('================================================================\n');
    fprintf(' pareto_explorer: %s\n', master_csv);
    fprintf('----------------------------------------------------------------\n');
    fprintf(' SIM TRUST: this CSV was produced by sim_pure_pursuit.\n');
    fprintf(' vehicle_params.WHEELBASE_M is now 0.957m (FITTED 2026-05-14\n');
    fprintf(' by calibrate_vehicle_params.py from real recordings), up from\n');
    fprintf(' USER_SIM 0.80m. Sim still lacks servo lag + Turn PID models,\n');
    fprintf(' so absolute XTE/duration may not match real-car numbers. Use\n');
    fprintf(' Pareto ORDERING for relative comparisons - do NOT read off\n');
    fprintf(' absolute values as on-car SET MISSION parameters.\n');
    fprintf('================================================================\n\n');
end


function style_axes(ax)
    set(ax, 'Color', [0.15 0.17 0.22], ...
            'XColor', 'w', 'YColor', 'w', 'ZColor', 'w', ...
            'GridColor', [0.4 0.4 0.4]);
end


function out = coerce_logical(col)
%COERCE_LOGICAL  Normalize a CSV-loaded boolean-ish column to logical.
%
%   The Python sweeper writes "True"/"False" via the default CSV writer,
%   so readtable can hand us either:
%     - a logical column (newest MATLAB autodetect)
%     - a string array
%     - a cellstr
%     - numeric 0/1
%   Convert all variants to a logical column we can compose with `~`.
    if islogical(col)
        out = col;
    elseif isnumeric(col)
        out = (col ~= 0);
    elseif isstring(col)
        out = (col == "True");
    elseif iscellstr(col)
        out = strcmp(col, 'True');
    elseif iscategorical(col)
        out = (col == "True");
    else
        error('pareto_explorer:bad_lost_type', ...
              'unsupported lost column type: %s', class(col));
    end
end
