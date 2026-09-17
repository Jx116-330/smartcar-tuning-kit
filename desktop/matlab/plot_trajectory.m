function fig = plot_trajectory(T, opts)
%PLOT_TRAJECTORY  Visualize a single recording: XY path + 4 time series.
%
%   fig = PLOT_TRAJECTORY(T) draws four subplots from a recording table
%   loaded by load_recording: the XY trajectory of the car
%   (engaged-frame segment colored), and time series of XTE, steer
%   command, and speed command. Returns the figure handle.
%
%   fig = PLOT_TRAJECTORY(T, opts) takes a struct with optional fields:
%       opts.title     - main figure title (default: 'Recording')
%       opts.show_full - if true, also plot st=0 idle frames in grey
%                        (default: true)
%       opts.save_path - if given, saveas(fig, opts.save_path)
%
%   The plotting style matches the dark-theme used in the user's
%   existing ackermann_steering_sim.m for visual consistency.
%
%   Pure data visualization - no kinematic-model assumptions. Safe to
%   use regardless of how trustworthy vehicle_params.py currently is.
%
%   Example:
%       T = load_recording('recordings/tmp_run_xte_test3.json');
%       plot_trajectory(T, ...
%           'title', 'xte_test3 baseline', ...
%           'save_path', 'xte_test3.png');
%       % NOTE: name-value (keyword) args, NOT a struct - the
%       % `arguments` block uses opts.field syntax which means MATLAB
%       % expects the caller to pass them by name.

    arguments
        T table
        opts.title     (1,:) char = 'Recording'
        opts.show_full (1,1) logical = true
        opts.save_path (1,:) char = ''
    end

    if height(T) == 0
        warning('plot_trajectory:empty', 'empty table, nothing to plot');
        fig = [];
        return
    end

    fig = figure('Name', opts.title, 'Position', [80, 80, 1280, 800], ...
                 'Color', [0.12 0.14 0.18]);

    eng_mask = (T.eng == 1);
    idle_mask = ~eng_mask;

    % Relative time axis. Build piecewise-monotonic time vector that
    % survives any car-reboot wrap in ms. Detect a reset as a negative
    % diff and patch by adding the previous segment's last value.
    rel_t = build_relative_time(double(T.ms));

    % ---- subplot 1: XY trajectory ------------------------------------
    ax1 = subplot(2, 2, 1, 'Parent', fig);
    hold(ax1, 'on'); grid(ax1, 'on'); axis(ax1, 'equal');
    if opts.show_full && any(idle_mask)
        plot(ax1, T.cpx(idle_mask), T.cpy(idle_mask), '.', ...
            'Color', [0.5 0.5 0.5], 'MarkerSize', 4, ...
            'DisplayName', 'idle (st\neq 3)');
    end
    if any(eng_mask)
        plot(ax1, T.cpx(eng_mask), T.cpy(eng_mask), '-', ...
            'Color', [0.0 0.95 0.95], 'LineWidth', 1.8, ...
            'DisplayName', 'engaged (st=3)');
        % Start and end markers for the engaged segment.
        eng_idx = find(eng_mask);
        plot(ax1, T.cpx(eng_idx(1)), T.cpy(eng_idx(1)), 'o', ...
            'MarkerSize', 9, 'MarkerFaceColor', [0.2 0.9 0.4], ...
            'MarkerEdgeColor', 'w', 'DisplayName', 'start');
        plot(ax1, T.cpx(eng_idx(end)), T.cpy(eng_idx(end)), 's', ...
            'MarkerSize', 9, 'MarkerFaceColor', [0.95 0.3 0.3], ...
            'MarkerEdgeColor', 'w', 'DisplayName', 'end');
    end
    xlabel(ax1, 'X (m)', 'Color', 'w');
    ylabel(ax1, 'Y (m)', 'Color', 'w');
    title(ax1, 'XY trajectory', 'Color', 'w');
    legend(ax1, 'Location', 'best', 'TextColor', 'w', 'Color', [0.2 0.22 0.27]);
    style_axes(ax1);

    % ---- subplot 2: XTE vs time --------------------------------------
    ax2 = subplot(2, 2, 2, 'Parent', fig);
    hold(ax2, 'on'); grid(ax2, 'on');
    if any(eng_mask)
        xte_cm = T.xte(eng_mask) * 100;
        t_eng  = rel_t(eng_mask);
        plot(ax2, t_eng, xte_cm, '-', 'Color', [0.95 0.45 0.55], ...
            'LineWidth', 1.4);
        yline(ax2, 0, '--', 'Color', [0.6 0.6 0.6]);
        % Annotate rms / max for quick reference.
        xte_valid = xte_cm(abs(xte_cm) > 1e-4 & ~isnan(xte_cm));
        if ~isempty(xte_valid)
            xte_rms = sqrt(mean(xte_valid .^ 2));
            xte_max = max(abs(xte_valid));
            title(ax2, sprintf('XTE (rms=%.2fcm, max=%.2fcm)', ...
                  xte_rms, xte_max), 'Color', 'w');
        else
            title(ax2, 'XTE (no data)', 'Color', 'w');
        end
    end
    xlabel(ax2, 'time (s)', 'Color', 'w');
    ylabel(ax2, 'XTE (cm)', 'Color', 'w');
    style_axes(ax2);

    % ---- subplot 3: steer command vs time ----------------------------
    ax3 = subplot(2, 2, 3, 'Parent', fig);
    hold(ax3, 'on'); grid(ax3, 'on');
    if any(eng_mask)
        plot(ax3, rel_t(eng_mask), T.str(eng_mask), '-', ...
            'Color', [0.65 0.55 0.95], 'LineWidth', 1.4);
        yline(ax3, 0, '--', 'Color', [0.6 0.6 0.6]);
        yline(ax3,  29, ':', 'Color', [0.95 0.3 0.3]);  % firmware clamp
        yline(ax3, -29, ':', 'Color', [0.95 0.3 0.3]);
    end
    xlabel(ax3, 'time (s)', 'Color', 'w');
    ylabel(ax3, 'steer cmd (deg)', 'Color', 'w');
    title(ax3, 'Steering output (\pm29deg = firmware clamp)', 'Color', 'w');
    style_axes(ax3);

    % ---- subplot 4: yaw error + lookahead distance --------------------
    ax4 = subplot(2, 2, 4, 'Parent', fig);
    hold(ax4, 'on'); grid(ax4, 'on');
    if any(eng_mask)
        yyaxis(ax4, 'left');
        plot(ax4, rel_t(eng_mask), T.yer(eng_mask), '-', ...
            'Color', [0.95 0.7 0.3], 'LineWidth', 1.3);
        ylabel(ax4, 'yaw err (deg)', 'Color', 'w');
        yyaxis(ax4, 'right');
        plot(ax4, rel_t(eng_mask), T.ladst(eng_mask), '-', ...
            'Color', [0.3 0.85 0.6], 'LineWidth', 1.0);
        ylabel(ax4, 'lookahead dist (m)', 'Color', 'w');
    end
    xlabel(ax4, 'time (s)', 'Color', 'w');
    title(ax4, 'Yaw error (left) and lookahead distance (right)', ...
          'Color', 'w');
    style_axes(ax4);

    % Top-level title with parameter context.
    if any(eng_mask)
        eng_first = find(eng_mask, 1, 'first');
        head = sprintf('%s   |   KP=%.1f  LA=%dmm  BLND=%.1f  pts=%d', ...
            opts.title, T.kp(eng_first), T.la(eng_first), ...
            T.blnd(eng_first), T.pts(eng_first));
    else
        head = sprintf('%s   |   no engaged frames', opts.title);
    end
    sgtitle(fig, head, 'Color', 'w', 'FontSize', 13, 'FontWeight', 'bold');

    if ~isempty(opts.save_path)
        saveas(fig, opts.save_path);
        fprintf('plot_trajectory: saved %s\n', opts.save_path);
    end
end


function style_axes(ax)
%STYLE_AXES  Apply the dark theme used elsewhere in E:/ads/*.m.
    set(ax, 'Color', [0.15 0.17 0.22], ...
            'XColor', 'w', 'YColor', 'w', ...
            'GridColor', [0.4 0.4 0.4]);
end


function rel = build_relative_time(ms)
%BUILD_RELATIVE_TIME  Reboot-safe relative time from car-side ms field.
%
%   ms is an unsigned uint32-ish counter that wraps on car reboot. We
%   detect each wrap (negative diff > 1s) and patch by adding the
%   running offset, so the returned time vector is monotonically
%   non-decreasing and reflects accumulated elapsed seconds.
    rel = zeros(numel(ms), 1);
    offset = 0;
    for i = 2:numel(ms)
        d = ms(i) - ms(i-1);
        if d < -1000
            % Negative jump > 1s: treat as a car reboot. Continue
            % monotonically by adding offset = previous tail.
            offset = offset + (ms(i-1) - ms(i)) + 50;
        end
        rel(i) = (ms(i) + offset - ms(1)) / 1000.0;
    end
end
