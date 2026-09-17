function M = compute_metrics(T, label)
%COMPUTE_METRICS  Reduce a recording table to the 4 scalar metrics.
%
%   M = COMPUTE_METRICS(T, label) takes a table T produced by
%   load_recording and returns a struct with the same fields produced
%   by the Python `replay_to_dataframe.compute_metrics` function, so
%   downstream MATLAB plots and Python-side analyses agree on what
%   "XTE rms" or "sign_flips" means for the same input.
%
%   The 4 metric families (matching the offline-optimization plan):
%       XTE        : xte_rms_m, xte_max_m, xte_mean_signed_m
%       Duration   : duration_s
%       Smoothness : steer_dstr_std_deg, sign_flips
%       Safety     : lost_fired, completion_ratio
%
%   Input:
%       T     - table from load_recording (must contain at least the
%               fields ms, st, eng, idx, xte, str, lost, kp, la,
%               blnd, pts)
%       label - char, identifies this run in the output (e.g. file
%               stem). Optional; defaults to 'run'.
%
%   The metric definitions match
%   replay_to_dataframe.py:compute_metrics() exactly. Any change to one
%   must be mirrored in the other or the two will disagree.
%
%   Example:
%       T = load_recording('recordings/tmp_run_xte_test3.json');
%       M = compute_metrics(T, 'xte_test3');
%       fprintf('XTE rms = %.2f cm\n', M.xte_rms_m * 100);

    arguments
        T table
        label (1,:) char = 'run'
    end

    M = struct( ...
        'file',                label, ...
        'n_frames',            height(T), ...
        'n_engaged',           0, ...
        'completion_ratio',    0, ...
        'duration_s',          0, ...
        'xte_rms_m',           NaN, ...
        'xte_max_m',           NaN, ...
        'xte_mean_signed_m',   NaN, ...
        'steer_dstr_std_deg',  NaN, ...
        'sign_flips',          0, ...
        'lost_fired',          false, ...
        'kp',                  0, ...
        'la',                  0, ...
        'blnd',                0, ...
        'pts',                 0 ...
    );

    if height(T) == 0
        return
    end

    eng = T(T.eng == 1, :);
    M.n_engaged = height(eng);
    if M.n_engaged == 0
        return
    end

    % Duration over engaged frames only. ms can wrap on car reboot, so
    % a single (ms_last - ms_first) is only reliable when no reboot
    % happened mid-engaged-segment; for engaged-only runs this is the
    % case in practice (eng=1 implies the mission state machine is in
    % RETURN, which doesn't survive a reboot).
    M.duration_s = double(eng.ms(end) - eng.ms(1)) / 1000.0;

    % XTE metric (mirror replay_to_dataframe.py:113-122). Drop near-zero
    % entries; they're frames before the cursor advanced past index 0
    % when xte is mathematically 0 anyway. NaN-aware in case xte field
    % was filled by load_recording.
    xte = eng.xte;
    xte_valid = xte(abs(xte) > 1e-6 & ~isnan(xte));
    if ~isempty(xte_valid)
        M.xte_rms_m         = sqrt(mean(xte_valid .^ 2));
        M.xte_max_m         = max(abs(xte_valid));
        M.xte_mean_signed_m = mean(xte_valid);
    end

    % Steer smoothness: std of first-difference of commanded steer.
    str = eng.str;
    if numel(str) >= 2
        dstr = diff(str);
        if ~isempty(dstr)
            M.steer_dstr_std_deg = std(dstr);
        end
        % Sign flips: count zero-crossings of str (excluding zeros).
        signs = sign(str);
        flips = 0;
        for i = 2:numel(signs)
            if signs(i) ~= 0 && signs(i-1) ~= 0 && signs(i) ~= signs(i-1)
                flips = flips + 1;
            end
        end
        M.sign_flips = flips;
    end

    % Safety: lost-path watchdog ever fired in this run?
    if ismember('lost', T.Properties.VariableNames)
        M.lost_fired = any(eng.lost == 1);
    end

    % Completion: how far along the recorded path did the cursor get.
    if ismember('pts', T.Properties.VariableNames) && eng.pts(1) > 0
        M.pts = double(eng.pts(1));
        last_idx = double(eng.idx(end));
        M.completion_ratio = (last_idx + 1) / M.pts;
    end

    % Parameter context (taken from first engaged frame).
    M.kp   = double(eng.kp(1));
    M.la   = double(eng.la(1));
    M.blnd = double(eng.blnd(1));
end
