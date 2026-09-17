function T = load_recording(path)
%LOAD_RECORDING  Load a TELMISSION recording JSON into a MATLAB table.
%
%   T = LOAD_RECORDING(path) reads a recording produced by either
%   record_session.py / tmp_capture.py (the legacy bare-array format) or
%   tuning_session.py (a wrapped {meta, frames, ...} object). Both
%   schemas are auto-detected. The returned table has one row per frame.
%
%   Each row carries the per-frame TELMISSION fields used elsewhere in
%   the toolchain:
%       ms     - car-side timestamp (uint32, ms since car boot - NOT
%                monotonic across car reboots, see note below)
%       st     - mission state (0=IDLE, 1=EVENT1, 2=LEARN, 3=RETURN,
%                4=ABORT)
%       eng    - actuator engaged (0/1)
%       idx    - cursor index along the recorded path
%       laidx  - lookahead point index
%       dst    - distance to cursor target (m)
%       ladst  - lookahead distance (m)
%       lageo  - geometric (atan2) aim yaw (deg)
%       larec  - recorded yaw at the lookahead point (deg)
%       yer    - yaw error = aim_yaw - cur_yaw (deg)
%       scmd   - speed command normalized
%       str    - steer output written to the servo (deg)
%       cyaw   - current vehicle heading (deg)
%       cpx    - current x position (m)
%       cpy    - current y position (m)
%       lost   - lost-path watchdog flag (0/1)
%       blnd   - HEAD_BLEND setting at this frame (0..1)
%       la     - LOOKAHEAD setting at this frame (mm)
%       kp     - STEER_KP setting at this frame
%       pts    - total recorded-path points
%       xte    - cross-track error (m, signed: +left / -right)
%
%   Note on car-side time:
%       The `ms` field is uint32 milliseconds since the car powered on.
%       It WILL reset to a small value if the car reboots mid-session
%       (see recordings/tmp_run_xte_curve_175pt.json for an example).
%       For monotonic time within a single run use the difference
%       (ms - ms(1)) ONLY over a stretch with no reset; do not assume
%       sortedness across the whole file.
%
%   Example:
%       T = load_recording('E:/ads/tcp_tool/recordings/tmp_run_xte_test3.json');
%       eng = T(T.eng == 1, :);
%       fprintf('engaged frames: %d / %d\n', height(eng), height(T));

    arguments
        path (1,:) char
    end

    raw = fileread(path);
    data = jsondecode(raw);

    % Two accepted shapes:
    %   1. array of structs (legacy tmp_capture.py / record_session.py)
    %   2. struct with .frames = array (tuning_session.py wrapped form,
    %      and arrays of mixed-shape objects sometimes deserialize as
    %      struct arrays directly)
    if isstruct(data) && isfield(data, 'frames')
        frames = data.frames;
    else
        frames = data;
    end

    if isempty(frames)
        T = table();
        warning('load_recording:empty', 'no frames in %s', path);
        return
    end

    % jsondecode returns either a struct array (when all rows have the
    % same field set) or a cell array (when fields differ). Normalize.
    if iscell(frames)
        % Collect the union of keys across all cells, then build a struct
        % array with missing fields defaulted to 0. This handles the
        % case where 'xte' was added mid-experiment (tmp_capture.py:63).
        all_keys = {};
        for k = 1:numel(frames)
            all_keys = union(all_keys, fieldnames(frames{k}));
        end
        unified = struct();
        for k = 1:numel(frames)
            for f = 1:numel(all_keys)
                key = all_keys{f};
                if isfield(frames{k}, key)
                    unified(k).(key) = frames{k}.(key);
                else
                    unified(k).(key) = 0;
                end
            end
        end
        T = struct2table(unified);
    else
        T = struct2table(frames);
    end

    % Standardize field presence: if 'xte' missing (older recordings),
    % fill with NaN so consumers can detect "no data" vs "zero".
    if ~ismember('xte', T.Properties.VariableNames)
        T.xte = nan(height(T), 1);
    end
end
