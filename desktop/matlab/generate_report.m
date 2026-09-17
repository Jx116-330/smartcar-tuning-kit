function pdf_path = generate_report(opts)
%GENERATE_REPORT  Build a one-shot PDF combining the MATLAB plots.
%
%   pdf_path = GENERATE_REPORT(opts) renders the dashboards from
%   compare_runs / plot_trajectory / pareto_explorer into a single
%   PDF using MATLAB's exportgraphics with 'Append' mode.
%
%   opts (all optional):
%       opts.runs_index   - path to runs/_index.jsonl
%                           (default: ../runs/_index.jsonl relative to
%                           this .m file)
%       opts.recording    - path to a single TELMISSION recording JSON
%                           to feature on its own trajectory page
%                           (default: most recent file in ../recordings/)
%       opts.sweep_csv    - path to a sweeps/.../master.csv for the
%                           Pareto-explorer page (default: '' = skip
%                           that page)
%       opts.output       - output PDF path (default:
%                           reports/<timestamp>_summary.pdf next to
%                           this .m file)
%
%   The report is laid out as:
%       Page 1: text cover with parameter-provenance summary and
%               sim-trust caveat
%       Page 2: compare_runs(runs_index) - all completed runs
%       Page 3: plot_trajectory(recording) - one detailed run
%       Page 4: pareto_explorer(sweep_csv) - sim Pareto, only if
%               sweep_csv was supplied
%
%   The function is conservative - if a path is missing or empty it
%   prints a warning and skips that page rather than aborting.
%
%   Example:
%       pdf_path = generate_report(struct( ...
%           'runs_index', 'E:/ads/tcp_tool/runs/_index.jsonl', ...
%           'recording',  'E:/ads/tcp_tool/recordings/tmp_run_xte_test3.json', ...
%           'sweep_csv',  ''));

    arguments
        opts.runs_index (1,:) char = ''
        opts.recording  (1,:) char = ''
        opts.sweep_csv  (1,:) char = ''
        opts.output     (1,:) char = ''
    end

    here = fileparts(mfilename('fullpath'));
    if isempty(opts.runs_index)
        opts.runs_index = fullfile(here, '..', 'runs', '_index.jsonl');
    end
    if isempty(opts.recording)
        rec_dir = fullfile(here, '..', 'recordings');
        files = dir(fullfile(rec_dir, '*.json'));
        files = files(~startsWith({files.name}, '_'));
        if ~isempty(files)
            [~, latest] = max([files.datenum]);
            opts.recording = fullfile(rec_dir, files(latest).name);
        end
    end
    if isempty(opts.output)
        reports_dir = fullfile(here, '..', 'reports');
        if ~isfolder(reports_dir), mkdir(reports_dir); end
        ts = datestr(now, 'yyyymmdd-HHMMSS');  %#ok<DATST>
        opts.output = fullfile(reports_dir, [ts, '_summary.pdf']);
    end
    pdf_path = opts.output;

    fprintf('generate_report: writing %s\n', pdf_path);
    if isfile(pdf_path), delete(pdf_path); end

    % --- Page 1: cover -------------------------------------------------
    cover = figure('Name', 'Report Cover', ...
                   'Position', [80, 80, 900, 1100], ...
                   'Color', 'w', 'Visible', 'off');
    ax = axes('Parent', cover); axis(ax, 'off');
    lines = {
        sprintf('TC387 T-kart Tuning Report'), ...
        sprintf('Generated: %s', datestr(now, 'yyyy-mm-dd HH:MM:SS')), ...   %#ok<DATST>
        '', ...
        'Sources used:', ...
        sprintf('  runs index : %s', opts.runs_index), ...
        sprintf('  recording  : %s', opts.recording), ...
        sprintf('  sweep csv  : %s', ifempty(opts.sweep_csv, '(skipped)')), ...
        '', ...
        'Vehicle parameter provenance', ...
        '  DATASHEET (PDF):  track 0.50m, front-r 0.10m, rear-r 0.12m,', ...
        '                    rear motor 1:18, no-load 5800 RPM,', ...
        '                    rated 0.37 N.m, steer motor 1:192', ...
        '  FIRMWARE       :  steer clamp 29deg, slow ratio 0.5,', ...
        '                    cruise dist 0.6m, reach 0.2m, lost-path 2m', ...
        '  USER confirmed :  mechanical max steer 30deg', ...
        '  USER_SIM       :  WHEELBASE 0.80m, cg-to-rear 0.35m,', ...
        '                    v_nom 2.0 m/s  <-- NOT verified, awaiting', ...
        '                    on-car tape measure', ...
        '  PLACEHOLDER    :  servo_tau, enc_ticks_per_mm,', ...
        '                    max_lateral_accel', ...
        '', ...
        'SIM TRUST GUARD', ...
        '  Pareto results in this report (if any) come from', ...
        '  param_sweep.py which uses WHEELBASE_M = 0.80m. That value', ...
        '  is USER_SIM not measured. Use Pareto for RELATIVE ordering', ...
        '  of parameter combinations only - do NOT set on-car', ...
        '  SET MISSION params from absolute sim numbers until WB is', ...
        '  measured.', ...
        '', ...
        'Recording-side analyses (compare_runs, plot_trajectory) are', ...
        'pure data and unaffected by the sim trust issue.'};
    text(ax, 0.05, 0.97, strjoin(lines, newline), 'FontName', 'Courier', ...
         'FontSize', 10, 'VerticalAlignment', 'top', ...
         'Interpreter', 'none');
    exportgraphics(cover, pdf_path, 'ContentType', 'vector');
    close(cover);

    % --- Page 2: compare_runs -----------------------------------------
    if isfile(opts.runs_index)
        try
            out = compare_runs(opts.runs_index);
            exportgraphics(out.fig, pdf_path, 'Append', true);
            close(out.fig);
        catch err
            warning('generate_report:compare_runs', ...
                    'skipped (error: %s)', err.message);
        end
    else
        warning('generate_report:no_runs', 'no runs_index at %s', ...
                opts.runs_index);
    end

    % --- Page 3: plot_trajectory --------------------------------------
    if isfile(opts.recording)
        try
            T = load_recording(opts.recording);
            [~, stem] = fileparts(opts.recording);
            fig = plot_trajectory(T, struct('title', stem));
            exportgraphics(fig, pdf_path, 'Append', true);
            close(fig);
        catch err
            warning('generate_report:plot_traj', ...
                    'skipped (error: %s)', err.message);
        end
    else
        warning('generate_report:no_rec', 'no recording at %s', ...
                opts.recording);
    end

    % --- Page 4: pareto_explorer --------------------------------------
    if ~isempty(opts.sweep_csv) && isfile(opts.sweep_csv)
        try
            res = pareto_explorer(opts.sweep_csv);
            exportgraphics(res.fig, pdf_path, 'Append', true);
            close(res.fig);
        catch err
            warning('generate_report:pareto', ...
                    'skipped (error: %s)', err.message);
        end
    end

    fprintf('generate_report: done -> %s\n', pdf_path);
end


function s = ifempty(x, fallback)
    if isempty(x), s = fallback; else, s = x; end
end
