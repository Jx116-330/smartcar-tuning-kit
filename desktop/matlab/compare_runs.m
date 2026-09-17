function out = compare_runs(index_path, opts)
%COMPARE_RUNS  Side-by-side analysis of multiple tuning-mode runs.
%
%   out = COMPARE_RUNS(index_path) reads runs/_index.jsonl (one JSON
%   line per completed run, produced by tuning_session.py) and returns
%   a struct with:
%       out.summary  - table of all runs (params + metrics + reason)
%       out.fig      - figure handle showing comparative plots
%
%   out = COMPARE_RUNS(index_path, opts) takes a struct with fields:
%       opts.max_runs   - limit to most recent N runs (default: 30)
%       opts.sort_by    - sort column for output table (default:
%                          'run_id'). Other useful keys: 'xte_rms_cm',
%                          'duration_s', 'completion'.
%       opts.save_path  - if set, saveas(fig, opts.save_path)
%
%   The figure has three panels:
%       1. Scatter of XTE rms vs duration, colored by KP, sized by LA.
%          Points labeled by run_id. Pareto-style at-a-glance overview.
%       2. Bar chart: each run's XTE rms, colored by end_reason
%          (COMPLETE = green, LOST_PATH = red, MANUAL_OR_OTHER = grey).
%       3. Parallel-coordinates: kp / la / blnd / xte_rms_cm /
%          completion. Spots a parameter sweep direction at a glance.
%
%   This is pure recording-data analysis. No vehicle_params used, so
%   the output is independent of the WHEELBASE_M UNVERIFIED issue.
%
%   Example:
%       out = compare_runs('E:/ads/tcp_tool/runs/_index.jsonl', ...
%                          struct('max_runs', 20));
%       disp(out.summary);

    arguments
        index_path (1,:) char
        opts.max_runs  (1,1) double = 30
        opts.sort_by   (1,:) char   = 'run_id'
        opts.save_path (1,:) char   = ''
    end

    if ~isfile(index_path)
        error('compare_runs:nofile', 'index not found: %s', index_path);
    end

    % Parse JSON-lines.
    text = fileread(index_path);
    lines = splitlines(strtrim(text));
    rows = cell(0);
    for k = 1:numel(lines)
        ln = strtrim(lines{k});
        if isempty(ln)
            continue
        end
        try
            r = jsondecode(ln);
        catch err
            warning('compare_runs:badline', ...
                'skipping malformed line %d: %s', k, err.message);
            continue
        end
        rows{end+1} = r;  %#ok<AGROW>
    end

    if isempty(rows)
        error('compare_runs:empty', 'no parseable runs in %s', index_path);
    end

    T = struct2table([rows{:}]);
    if height(T) > opts.max_runs
        T = T(end - opts.max_runs + 1 : end, :);
    end

    if ismember(opts.sort_by, T.Properties.VariableNames)
        T = sortrows(T, opts.sort_by);
    end

    % --- Figure -------------------------------------------------------
    fig = figure('Name', 'Tuning Runs Comparison', ...
                 'Position', [80, 80, 1400, 800], ...
                 'Color', [0.12 0.14 0.18]);

    % Panel 1: scatter XTE vs duration, color KP, size LA.
    ax1 = subplot(1, 3, 1, 'Parent', fig);
    hold(ax1, 'on'); grid(ax1, 'on');
    sizes = max(20, double(T.la) / 10);   % scale LA(mm) into marker size
    scatter(ax1, T.duration_s, T.xte_rms_cm, sizes, T.kp, 'filled', ...
            'MarkerEdgeColor', 'w', 'LineWidth', 0.5);
    for i = 1:height(T)
        text(ax1, T.duration_s(i), T.xte_rms_cm(i), ...
             sprintf(' #%d', T.run_id(i)), 'Color', 'w', 'FontSize', 8);
    end
    cb = colorbar(ax1); cb.Color = 'w'; cb.Label.String = 'KP';
    cb.Label.Color = 'w';
    xlabel(ax1, 'duration (s)', 'Color', 'w');
    ylabel(ax1, 'XTE rms (cm)', 'Color', 'w');
    title(ax1, 'XTE vs duration (marker size = LA)', 'Color', 'w');
    style_axes(ax1);

    % Panel 2: XTE bar chart colored by reason.
    ax2 = subplot(1, 3, 2, 'Parent', fig);
    hold(ax2, 'on'); grid(ax2, 'on');
    reasons = string(T.reason);
    colors = zeros(height(T), 3);
    for i = 1:height(T)
        switch reasons(i)
            case "COMPLETE",         colors(i, :) = [0.2 0.85 0.4];
            case "LOST_PATH",        colors(i, :) = [0.95 0.3 0.3];
            case "BRAKE",            colors(i, :) = [0.95 0.65 0.2];
            case "DIR_KEY_OFF",      colors(i, :) = [0.95 0.85 0.3];
            case "NO_ENGAGE",        colors(i, :) = [0.5 0.5 0.5];
            otherwise,               colors(i, :) = [0.6 0.6 0.7];
        end
    end
    for i = 1:height(T)
        bar(ax2, i, T.xte_rms_cm(i), 'FaceColor', colors(i, :), ...
            'EdgeColor', 'w', 'LineWidth', 0.5);
    end
    xticks(ax2, 1:height(T));
    xticklabels(ax2, arrayfun(@(r) sprintf('#%d', r), T.run_id, ...
                              'UniformOutput', false));
    xtickangle(ax2, 60);
    ylabel(ax2, 'XTE rms (cm)', 'Color', 'w');
    title(ax2, 'XTE per run (color = end\_reason)', 'Color', 'w');
    style_axes(ax2);

    % Panel 3: parallel coordinates kp / la / blnd / xte / completion.
    ax3 = subplot(1, 3, 3, 'Parent', fig);
    hold(ax3, 'on'); grid(ax3, 'on');
    axes_names = {'KP', 'LA', 'BLND', 'XTE(cm)', 'COMP'};
    % Normalize columns to [0, 1] for parallel-coords readability.
    cols = [T.kp, double(T.la), T.blnd, T.xte_rms_cm, T.completion];
    norm_cols = zeros(size(cols));
    for c = 1:size(cols, 2)
        v = cols(:, c);
        lo = min(v); hi = max(v);
        if hi - lo < eps
            norm_cols(:, c) = 0.5;
        else
            norm_cols(:, c) = (v - lo) / (hi - lo);
        end
    end
    x_axis = 1:size(cols, 2);
    cmap = parula(height(T));
    for i = 1:height(T)
        plot(ax3, x_axis, norm_cols(i, :), '-o', ...
            'Color', cmap(i, :), 'LineWidth', 1.4, 'MarkerSize', 6, ...
            'MarkerFaceColor', cmap(i, :));
    end
    xticks(ax3, x_axis);
    xticklabels(ax3, axes_names);
    ylabel(ax3, 'normalized', 'Color', 'w');
    title(ax3, 'Parallel coordinates (each line = one run)', ...
          'Color', 'w');
    style_axes(ax3);

    sgtitle(fig, sprintf('Tuning Runs Comparison  (n=%d)', height(T)), ...
            'Color', 'w', 'FontSize', 14, 'FontWeight', 'bold');

    if ~isempty(opts.save_path)
        saveas(fig, opts.save_path);
        fprintf('compare_runs: saved %s\n', opts.save_path);
    end

    out = struct('summary', T, 'fig', fig);
end


function style_axes(ax)
    set(ax, 'Color', [0.15 0.17 0.22], ...
            'XColor', 'w', 'YColor', 'w', ...
            'GridColor', [0.4 0.4 0.4]);
end
