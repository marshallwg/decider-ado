*! version 0.1.0  25nov2026
*! decider: Structured probabilistic decisions over text variables in Stata
*! Author: Marshall Garland

program define decider, rclass
    version 16.0

    // Dispatch subcommands: check, setup, resume, or standard scoring
    gettoken first rest : 0, parse(" ,")
    if ("`first'" == "check") {
        _decider_check `rest'
        return add
        exit
    }
    if ("`first'" == "setup") {
        _decider_setup `rest'
        return add
        exit
    }
    if ("`first'" == "resume") {
        _decider_resume `rest'
        return add
        exit
    }

    _decider_score `0'
    return add
end


/*==============================================================================
  _decider_resolve_python: Pick user override, managed ~/.decider/venv (for
  neojev), or OS-appropriate system Python (python on Windows, python3 on Unix)
==============================================================================*/
program define _decider_resolve_python, rclass
    version 16.0
    syntax [, PYthon(string) BACKend(string)]

    if (`"`python'"' != "") {
        return local python `"`python'"'
        exit
    }

    if ("`backend'" == "neojev") {
        local home : environment HOME
        if ("`home'" != "") {
            local managed "`home'/.decider/venv/bin/python"
            capture confirm file `"`managed'"'
            if (_rc == 0) {
                return local python `"`managed'"'
                exit
            }
        }
    }

    if ("`c(os)'" == "Windows") {
        return local python "python"
    }
    else {
        return local python "python3"
    }
end


/*==============================================================================
  _decider_check: Diagnose readiness for remote (OpenRouter) and local (NeoJev)
==============================================================================*/
program define _decider_check, rclass
    version 16.0
    syntax [, PYthon(string) VENdordir(string) MODel(string)]

    capture quietly findfile _decider_score.py
    if (_rc != 0) {
        di as err "Could not find _decider_score.py along adopath."
        exit 601
    }
    local py_script `"`r(fn)'"'

    _decider_resolve_python, python(`"`python'"') backend("openrouter")
    local py_exe `"`r(python)'"'

    tempfile chk_out chk_err
    local extra ""
    if (`"`vendordir'"' != "") local extra `"`extra' --vendor-dir "`vendordir'""'
    if (`"`model'"' != "")     local extra `"`extra' --model "`model'""'
    if (`"`python'"' != "")    local extra `"`extra' --local-python "`python'""'

    shell "`py_exe'" "`py_script'" --check --check-out "`chk_out'" `extra' 2> "`chk_err'"

    capture confirm file `"`chk_out'"'
    if (_rc != 0) {
        di as err "decider check could not execute Python (`py_exe')."
        di as err "Install Python 3.10+ or pass python(path). See README for setup instructions."
        capture type `"`chk_err'"'
        exit 198
    }

    tempname fh
    file open `fh' using `"`chk_out'"', read text
    file read `fh' line
    while (r(eof) == 0) {
        local eqpos = strpos(`"`line'"', "=")
        if (`eqpos' > 0) {
            local key = trim(substr(`"`line'"', 1, `eqpos' - 1))
            local val = trim(substr(`"`line'"', `eqpos' + 1, .))
            local c_`key' `"`val'"'
        }
        file read `fh' line
    }
    file close `fh'

    local rem_str = cond("`c_remote_ready'" == "1", "READY", "NOT READY")
    local loc_str = cond("`c_local_ready'"  == "1", "READY", "NOT READY")

    di as text _n "{hline 62}"
    di as text "decider environment check"
    di as text "{hline 62}"
    di as text "  System             : " as result "`c_os' (`c_arch')"
    di as text "  Python             : " as result "`c_python_version'" as text " (`c_python_exe')"
    di as text "  Remote (openrouter): " as result "`rem_str'"
    if ("`c_has_api_key'" != "1") {
        di as text "    - OPENROUTER_API_KEY not detected. Run: " as input "decider setup, openrouter"
    }
    di as text "  Local  (neojev)    : " as result "`loc_str'"
    if ("`c_apple_silicon'" != "1") {
        di as text "    - Requires Apple Silicon Mac (Darwin arm64); use backend(openrouter)."
    }
    else {
        if ("`c_has_mlx'" != "1" | "`c_has_gpu'" != "1") {
            di as text "    - MLX GPU environment not found (`c_local_python')."
        }
        if ("`c_has_vendor'" != "1") {
            di as text "    - NeoJev helper scripts (decider.py) not found."
        }
        if ("`c_has_weights'" != "1") {
            di as text "    - Model weights (`c_model') not cached locally."
        }
        if ("`c_local_ready'" != "1") {
            di as text "    - To install locally, run: " as input "decider setup, local downloadmodel"
        }
    }
    di as text "{hline 62}"
    di as text `"Setup guide: {browse "https://github.com/marshallwg/decider-ado#setup":github.com/marshallwg/decider-ado#setup}"'

    return scalar remote_ready  = real("`c_remote_ready'")
    return scalar local_ready   = real("`c_local_ready'")
    return scalar apple_silicon = real("`c_apple_silicon'")
    return scalar has_api_key   = real("`c_has_api_key'")
    return scalar has_mlx       = real("`c_has_mlx'")
    return scalar has_gpu       = real("`c_has_gpu'")
    return scalar has_vendor    = real("`c_has_vendor'")
    return scalar has_weights   = real("`c_has_weights'")
    return local  cmd           "decider"
    return local  subcmd        "check"
    return local  os            "`c_os'"
    return local  arch          "`c_arch'"
    return local  python_version "`c_python_version'"
    return local  python_exe    `"`c_python_exe'"'
    return local  local_python  `"`c_local_python'"'
    return local  vendor_dir    `"`c_vendor_dir'"'
end


/*==============================================================================
  _decider_setup: Guided setup for local NeoJev or remote OpenRouter
==============================================================================*/
program define _decider_setup, rclass
    version 16.0
    syntax [, LOCal OPENrouter DOWNloadmodel MODel(string) PYthon(string)]

    if ("`local'" == "" & "`openrouter'" == "") {
        di as err "Specify either local or openrouter (e.g., `decider setup, openrouter` or `decider setup, local downloadmodel`)."
        exit 198
    }
    if ("`local'" != "" & "`openrouter'" != "") {
        di as err "Specify only one of local or openrouter per call."
        exit 198
    }

    capture quietly findfile _decider_score.py
    if (_rc != 0) {
        di as err "Could not find _decider_score.py along adopath."
        exit 601
    }
    local py_script `"`r(fn)'"'
    _decider_resolve_python, python(`"`python'"') backend("openrouter")
    local py_exe `"`r(python)'"'

    if ("`openrouter'" != "") {
        shell "`py_exe'" "`py_script'" --setup-openrouter
    }
    else {
        local extra "--setup-local"
        if ("`downloadmodel'" != "") local extra `"`extra' --download-model"'
        if (`"`model'"' != "")       local extra `"`extra' --model "`model'""'
        shell "`py_exe'" "`py_script'" `extra'
    }

    local tgt = cond("`local'" != "", "local", "openrouter")
    return local cmd    "decider"
    return local subcmd "setup"
    return local target "`tgt'"
end


/*==============================================================================
  _decider_score: Prepare inputs in a frame, invoke Python engine, merge results
==============================================================================*/
program define _decider_score, rclass
    version 16.0

    syntax varlist(string) [if] [in], ///
        Type(string) ///
        PREfix(name) ///
        [ ///
            PROMPT(string asis) ///
            PROMPTFile(string) ///
            OPTions(string asis) ///
            BACKend(string) ///
            ALLOWcloud ///
            MODel(string) ///
            REVersecheck ///
            MINProb(real 0.80) ///
            MINMargin(real 0.30) ///
            THReshold(real 0.80) ///
            RUNdir(string) ///
            PYthon(string) ///
            VENdordir(string) ///
            TOKENbudget(integer 4096) ///
            TIMEout(integer 60) ///
            WORKers(integer 1) ///
            REPlace ///
        ]

    // ---- 1. Validate arguments before touching disk or calling Python -------
    local qtype = lower(trim("`type'"))
    if !inlist("`qtype'", "choice", "score", "noul") {
        di as err "type() must be one of: choice, score, noul"
        exit 198
    }

    if ("`backend'" == "") local backend "neojev"
    local backend = lower(trim("`backend'"))
    if !inlist("`backend'", "neojev", "openrouter", "mock") {
        di as err "backend() must be one of: neojev, openrouter, mock"
        exit 198
    }

    if ("`backend'" == "openrouter" & "`allowcloud'" == "") {
        di as err "Cloud transmission is disabled by default."
        di as err "Specify option allowcloud with backend(openrouter) only if this dataset may leave your machine."
        exit 198
    }

    if (`workers' < 1) {
        di as err "workers() must be a positive integer."
        exit 198
    }
    if ("`backend'" == "neojev" & `workers' > 1) {
        di as err "workers() > 1 is not supported for backend(neojev) (local MLX is GPU-bound and single-threaded)."
        exit 198
    }

    if (`"`prompt'"' == "" & "`promptfile'" == "") {
        di as err "Specify either prompt() or promptfile()."
        exit 198
    }
    if (`"`prompt'"' != "" & "`promptfile'" != "") {
        di as err "Specify only one of prompt() or promptfile(), not both."
        exit 198
    }
    if ("`promptfile'" != "") {
        confirm file `"`promptfile'"'
    }

    if ("`qtype'" == "noul" & "`reversecheck'" != "") {
        di as err "reversecheck is only valid with type(choice) or type(score)."
        exit 198
    }

    // Parse options() into numbered locals opt_1 .. opt_K
    local n_opts = 0
    local rest `"`options'"'
    while (`"`rest'"' != "") {
        gettoken this_opt rest : rest
        if (`"`this_opt'"' != "") {
            local ++n_opts
            local opt_`n_opts' `"`this_opt'"'
        }
    }
    if inlist("`qtype'", "choice", "score") {
        if (`n_opts' < 2) {
            di as err "type(`qtype') requires at least two labels in options()."
            exit 198
        }
        if (`n_opts' > 16) {
            di as err "At most 16 options are supported."
            exit 198
        }
    }
    else if (`n_opts' > 0) {
        di as err "options() is not allowed with type(noul)."
        exit 198
    }

    // Mark if/in sample WITHOUT dropping empty strings (missing text is flagged, not dropped)
    marksample touse, novarlist
    quietly count if `touse'
    local N_sample = r(N)
    if (`N_sample' == 0) {
        di as err "No observations selected."
        exit 2000
    }

    // Check target variable names before running inference
    _decider_target_vars, type("`qtype'") prefix("`prefix'") nopts(`n_opts') `reversecheck'
    local target_vars `"`r(vars)'"'
    if ("`replace'" == "") {
        foreach v of local target_vars {
            capture confirm variable `v'
            if (_rc == 0) {
                di as err "Variable `v' already exists. Use replace or choose another prefix()."
                exit 110
            }
        }
    }

    // Locate bundled Python scoring script and resolve Python executable
    capture quietly findfile _decider_score.py
    if (_rc != 0) {
        di as err "Could not find _decider_score.py along adopath."
        exit 601
    }
    local py_script `"`r(fn)'"'
    _decider_resolve_python, python(`"`python'"') backend("`backend'")
    local python `"`r(python)'"'

    // ---- 2. Prepare run directory and handoff files -------------------------
    if ("`rundir'" == "") {
        local ts = subinstr("`c(current_date)'_`c(current_time)'", " ", "", .)
        local ts = subinstr("`ts'", ":", "", .)
        local rnum = floor(runiform() * 90000 + 10000)
        local tmpbase = regexr("`c(tmpdir)'", "[/\\]+$", "")
        local rundir `"`tmpbase'/decider_`ts'_`rnum'"'
    }
    capture mkdir `"`rundir'"'
    capture confirm file `"`rundir'/scores.csv"'
    if (_rc == 0) {
        di as err "`rundir'/scores.csv already exists. Use `decider resume, rundir(...)` or a new rundir()."
        exit 602
    }

    // Write options.txt (one raw label per line)
    if (`n_opts' > 0) {
        tempname fh_opt
        quietly file open `fh_opt' using `"`rundir'/options.txt"', write text replace
        forvalues k = 1/`n_opts' {
            file write `fh_opt' `"`opt_`k''"' _n
        }
        file close `fh_opt'
    }

    // Write inline prompt.txt when prompt() is used
    if (`"`prompt'"' != "") {
        tempname fh_pr
        quietly file open `fh_pr' using `"`rundir'/prompt.txt"', write text replace
        file write `fh_pr' `"`prompt'"' _n
        file close `fh_pr'
    }

    local allow_flag = cond("`allowcloud'" != "", 1, 0)
    local rev_flag   = cond("`reversecheck'" != "", 1, 0)

    // Write flat key=value config_raw.txt (avoids JSON escaping hazards in Stata)
    tempname fh_cfg
    quietly file open `fh_cfg' using `"`rundir'/config_raw.txt"', write text replace
    file write `fh_cfg' "type=`qtype'" _n
    file write `fh_cfg' "prefix=`prefix'" _n
    file write `fh_cfg' "varlist=`varlist'" _n
    file write `fh_cfg' "backend=`backend'" _n
    file write `fh_cfg' "allow_cloud=`allow_flag'" _n
    file write `fh_cfg' "model=`model'" _n
    file write `fh_cfg' "prompt_file=`promptfile'" _n
    file write `fh_cfg' "reverse_check=`rev_flag'" _n
    file write `fh_cfg' "min_prob=`minprob'" _n
    file write `fh_cfg' "min_margin=`minmargin'" _n
    file write `fh_cfg' "threshold=`threshold'" _n
    file write `fh_cfg' "token_budget=`tokenbudget'" _n
    file write `fh_cfg' "timeout_seconds=`timeout'" _n
    file write `fh_cfg' "workers=`workers'" _n
    file write `fh_cfg' "vendor_dir=`vendordir'" _n
    file close `fh_cfg'

    // Export marked sample from a temporary frame (preserves strL, never alters active data)
    tempvar _rowid
    quietly gen long `_rowid' = _n

    tempname fr_in
    frame put `_rowid' `varlist' if `touse', into(`fr_in')
    frame `fr_in' {
        rename `_rowid' _decider_rowid
        quietly export delimited using `"`rundir'/input.csv"', quote replace
    }
    frame drop `fr_in'

    // ---- 3. Execute Python scoring engine via subprocess --------------------
    if ("`backend'" == "openrouter") {
        di as text "Cloud mode (OpenRouter): transmitting `varlist' for `N_sample' observations..."
    }
    capture erase `"`rundir'/summary.txt"'
    capture erase `"`rundir'/_stderr.log"'

    shell "`python'" "`py_script'" "`rundir'" 2> "`rundir'/_stderr.log"

    capture confirm file `"`rundir'/summary.txt"'
    if (_rc != 0) {
        di as err "decider scoring failed. Python output:"
        capture type `"`rundir'/_stderr.log"'
        exit 198
    }

    // ---- 4. Merge scores back via frame link and compute r() results --------
    _decider_merge_and_report, ///
        rundir(`"`rundir'"') ///
        touse(`touse') ///
        rowid(`_rowid') ///
        subcmd("score") ///
        `replace'
    return add
end


/*==============================================================================
  _decider_resume: Resume an interrupted run and attach results to active data
==============================================================================*/
program define _decider_resume, rclass
    version 16.0

    syntax, RUNdir(string) ///
        [ ///
            TIMEout(integer -1) ///
            WORKers(integer -1) ///
            PYthon(string) ///
            REPlace ///
        ]

    confirm file `"`rundir'/config.json"'
    confirm file `"`rundir'/model_input.json"'

    capture quietly findfile _decider_score.py
    if (_rc != 0) {
        di as err "Could not find _decider_score.py along adopath."
        exit 601
    }
    local py_script `"`r(fn)'"'
    _decider_resolve_python, python(`"`python'"') backend("neojev")
    local python `"`r(python)'"'

    // Verify saved positions against current text before any model calls or
    // replacement of existing results. Export strings through the same CSV path
    // as initial scoring; Python selects only the frozen input variable names.
    tempfile current_input
    if (_N > 0) {
        quietly ds, has(type string)
        local current_strings `r(varlist)'
        tempvar current_rowid
        quietly gen long `current_rowid' = _n
        tempname fr_current
        frame put `current_rowid' `current_strings', into(`fr_current')
        frame `fr_current' {
            rename `current_rowid' _decider_rowid
            quietly export delimited using `"`current_input'"', quote replace
        }
        frame drop `fr_current'
    }

    local extra_args "--resume"
    if (_N > 0) local extra_args `"`extra_args' --current-input "`current_input'""'
    if (`timeout' > 0) local extra_args `"`extra_args' --timeout `timeout'"'
    if (`workers' > 0) local extra_args `"`extra_args' --workers `workers'"'

    capture erase `"`rundir'/summary.txt"'
    capture erase `"`rundir'/_stderr.log"'

    shell "`python'" "`py_script'" "`rundir'" `extra_args' 2> "`rundir'/_stderr.log"

    capture confirm file `"`rundir'/summary.txt"'
    if (_rc != 0) {
        di as err "decider resume failed. Python output:"
        capture noisily type `"`rundir'/_stderr.log"'
        exit 198
    }

    // Attach to active dataset using original row_id == _n (if dataset is loaded)
    if (_N > 0) {
        tempvar _rowid _touse
        quietly gen long `_rowid' = _n
        quietly gen byte `_touse' = 0
        _decider_merge_and_report, ///
            rundir(`"`rundir'"') ///
            touse(`_touse') ///
            rowid(`_rowid') ///
            subcmd("resume") ///
            `replace'
        return add
    }
    else {
        // No dataset in memory: load scores.csv directly
        quietly import delimited using `"`rundir'/scores.csv"', ///
            varnames(1) bindquotes(strict) maxquotedrows(unlimited) clear
        di as text "No dataset was in memory; loaded completed scores from `rundir'/scores.csv."
        return local cmd "decider"
        return local subcmd "resume"
        return local run_dir `"`rundir'"'
    }
end


/*==============================================================================
  _decider_target_vars: Return exact list of variables to be created
==============================================================================*/
program define _decider_target_vars, rclass
    version 16.0
    syntax, Type(string) PREfix(name) Nopts(integer) [REVersecheck]

    local vars ""
    if ("`type'" == "noul") {
        local vars "`prefix'_prob `prefix'_status `prefix'_review `prefix'_review_reason"
    }
    else {
        local vars "`prefix'_class"
        forvalues k = 1/`nopts' {
            local vars "`vars' `prefix'_p`k'"
        }
        local vars "`vars' `prefix'_top_p `prefix'_margin"
        if ("`type'" == "score") {
            local vars "`vars' `prefix'_expected"
        }
        if ("`reversecheck'" != "") {
            local vars "`vars' `prefix'_rev_class `prefix'_order_stable"
        }
        local vars "`vars' `prefix'_status `prefix'_review `prefix'_review_reason"
    }
    return local vars `"`vars'"'
end


/*==============================================================================
  _decider_merge_and_report: Import scores.csv into a frame, assertively link,
  label variables, display summary, and populate all r() returned results
==============================================================================*/
program define _decider_merge_and_report, rclass
    version 16.0
    syntax, RUNdir(string) TOuse(varname) ROwid(varname) SUBcmd(string) [REPlace]

    // ---- 1. Parse summary.txt into locals -----------------------------------
    tempname fh_sum
    file open `fh_sum' using `"`rundir'/summary.txt"', read text
    file read `fh_sum' line
    while (r(eof) == 0) {
        local eqpos = strpos(`"`line'"', "=")
        if (`eqpos' > 0) {
            local key = trim(substr(`"`line'"', 1, `eqpos' - 1))
            local val = trim(substr(`"`line'"', `eqpos' + 1, .))
            local s_`key' `"`val'"'
        }
        file read `fh_sum' line
    }
    file close `fh_sum'

    local qtype        "`s_type'"
    local prefix       "`s_prefix'"
    local varlist      "`s_varlist'"
    local backend      "`s_backend'"
    local model        "`s_model'"
    local ret_model    "`s_returned_model'"
    local prompt_sha   "`s_prompt_sha256'"
    local prompt_file  "`s_prompt_file'"
    local rev_chk      = real("`s_reverse_check'")
    local min_prob     = real("`s_min_prob'")
    local min_margin   = real("`s_min_margin'")
    local threshold    = real("`s_threshold'")
    local workers      = real("`s_workers'")
    local n_opts       = real("`s_n_options'")
    local n_requests   = real("`s_n_requests'")
    local has_cost     = ("`s_cost_usd'" != "")
    if (`has_cost') local cost_usd = real("`s_cost_usd'")

    // Read option labels if present
    if (`n_opts' > 0) {
        tempname fh_o
        file open `fh_o' using `"`rundir'/options.txt"', read text
        local k = 0
        file read `fh_o' oline
        while (r(eof) == 0) {
            if (`"`oline'"' != "") {
                local ++k
                local opt_`k' `"`oline'"'
            }
            file read `fh_o' oline
        }
        file close `fh_o'
    }

    local rev_opt = cond(`rev_chk' == 1, "reversecheck", "")
    _decider_target_vars, type("`qtype'") prefix("`prefix'") nopts(`n_opts') `rev_opt'
    local created_vars `"`r(vars)'"'

    // Drop existing target vars only if replace was specified
    foreach v of local created_vars {
        capture confirm variable `v'
        if (_rc == 0) {
            if ("`replace'" != "") {
                drop `v'
            }
            else {
                di as err "Variable `v' already exists. Specify replace to overwrite."
                exit 110
            }
        }
    }

    // ---- 2. Load scores.csv into a temporary frame with strict types --------
    tempname fr_out
    frame create `fr_out'
    frame `fr_out' {
        quietly import delimited using `"`rundir'/scores.csv"', ///
            varnames(1) bindquotes(strict) maxquotedrows(unlimited) clear

        // Ensure row_id is long and unique
        capture destring row_id, replace
        recast long row_id
        isid row_id

        // Convert status text to labeled numeric byte (1 = scored, 2 = missing_text)
        quietly gen byte `prefix'_status = .
        quietly replace `prefix'_status = 1 if status == "scored"
        quietly replace `prefix'_status = 2 if status == "missing_text"
        assert inlist(`prefix'_status, 1, 2)

        // Review flag and reason
        capture destring review, replace force
        quietly gen byte `prefix'_review = review
        quietly gen strL `prefix'_review_reason = ""
        capture confirm string variable review_reason
        if (_rc == 0) {
            quietly replace `prefix'_review_reason = review_reason
        }

        if ("`qtype'" == "noul") {
            capture destring prob, replace force
            quietly gen double `prefix'_prob = prob
        }
        else {
            capture destring class_idx, replace force
            quietly gen int `prefix'_class = class_idx
            forvalues k = 1/`n_opts' {
                capture destring p`k', replace force
                quietly gen double `prefix'_p`k' = p`k'
            }
            capture destring top_p margin, replace force
            quietly gen double `prefix'_top_p = top_p
            quietly gen double `prefix'_margin = margin
            if ("`qtype'" == "score") {
                capture destring expected, replace force
                quietly gen double `prefix'_expected = expected
            }
            if (`rev_chk' == 1) {
                capture destring rev_class_idx order_stable, replace force
                quietly gen int `prefix'_rev_class = rev_class_idx
                quietly gen byte `prefix'_order_stable = order_stable
            }
        }

        keep row_id `created_vars'
    }

    // ---- 3. Assertively link scores back into active dataset ----------------
    tempvar _link
    quietly frlink 1:1 `rowid', frame(`fr_out' row_id) generate(`_link')

    if ("`subcmd'" == "score") {
        // Every marked observation must match; unmarked observations must not match
        assert !missing(`_link') if `touse'
        assert missing(`_link') if !`touse'
    }
    else {
        // On resume, mark matched rows as the sample
        quietly replace `touse' = !missing(`_link')
        quietly count if `touse'
        local n_matched = r(N)
        frame `fr_out': local n_scored_file = _N
        if (`n_matched' != `n_scored_file') {
            frame drop `fr_out'
            di as err "Resume mismatch: `n_scored_file' rows in scores.csv, but only `n_matched' matched active dataset."
            exit 459
        }
    }

    quietly frget `created_vars', from(`_link')
    drop `_link'
    frame drop `fr_out'

    // ---- 4. Apply variable labels, value labels, and provenance chars -------
    label define `prefix'_status_lbl 1 "scored" 2 "missing_text", replace
    label values `prefix'_status `prefix'_status_lbl
    label variable `prefix'_status "Decider scoring status (`prefix')"

    label define `prefix'_review_lbl 0 "No" 1 "Yes", replace
    label values `prefix'_review `prefix'_review_lbl
    label variable `prefix'_review "Decider review flag (`prefix')"
    label variable `prefix'_review_reason "Decider review reason (`prefix')"

    if ("`qtype'" == "noul") {
        label variable `prefix'_prob "Decider probability statement is true (`prefix')"
    }
    else {
        capture label drop `prefix'_class_lbl
        forvalues k = 1/`n_opts' {
            label define `prefix'_class_lbl `k' `"`opt_`k''"', add
            label variable `prefix'_p`k' `"P(`opt_`k'')"'
        }
        label values `prefix'_class `prefix'_class_lbl
        label variable `prefix'_class "Decider selected option (`prefix')"
        label variable `prefix'_top_p "Decider highest option probability (`prefix')"
        label variable `prefix'_margin "Decider top minus second probability (`prefix')"
        if ("`qtype'" == "score") {
            label variable `prefix'_expected "Decider expected level index (`prefix')"
        }
        if (`rev_chk' == 1) {
            label values `prefix'_rev_class `prefix'_class_lbl
            label variable `prefix'_rev_class "Decider reversed-order option (`prefix')"
            label define `prefix'_stable_lbl 0 "Unstable" 1 "Stable", replace
            label values `prefix'_order_stable `prefix'_stable_lbl
            label variable `prefix'_order_stable "Decider option-order stability (`prefix')"
        }
    }

    // Attach provenance characteristics to every created variable
    foreach v of local created_vars {
        char `v'[decider_backend]       `"`backend'"'
        char `v'[decider_model]         `"`model'"'
        char `v'[decider_prompt_sha256] `"`prompt_sha'"'
        char `v'[decider_run_dir]       `"`rundir'"'
    }

    // ---- 5. Compute summary statistics and r(dist) matrix -------------------
    quietly count if `touse'
    local N_total = r(N)
    quietly count if `touse' & `prefix'_status == 1
    local N_scored = r(N)
    quietly count if `touse' & `prefix'_status == 2
    local N_missing = r(N)
    quietly count if `touse' & `prefix'_review == 1
    local N_flagged = r(N)
    local share_flagged = cond(`N_total' > 0, `N_flagged' / `N_total', .)

    if ("`qtype'" == "noul") {
        quietly summarize `prefix'_prob if `touse' & `prefix'_status == 1, meanonly
        local mean_prob = cond(r(N) > 0, r(mean), .)
    }
    else {
        quietly summarize `prefix'_top_p if `touse' & `prefix'_status == 1, meanonly
        local mean_top_p = cond(r(N) > 0, r(mean), .)
        quietly summarize `prefix'_margin if `touse' & `prefix'_status == 1, meanonly
        local mean_margin = cond(r(N) > 0, r(mean), .)

        if (`rev_chk' == 1) {
            quietly count if `touse' & `prefix'_status == 1 & `prefix'_order_stable == 0
            local N_unstable = r(N)
            local share_stable = cond(`N_scored' > 0, (`N_scored' - `N_unstable') / `N_scored', .)
        }

        tempname D
        matrix `D' = J(`n_opts', 3, .)
        matrix colnames `D' = N Share Mean_Prob
        local rnames ""
        forvalues k = 1/`n_opts' {
            quietly count if `touse' & `prefix'_status == 1 & `prefix'_class == `k'
            local nk = r(N)
            matrix `D'[`k', 1] = `nk'
            matrix `D'[`k', 2] = cond(`N_scored' > 0, `nk' / `N_scored', .)
            quietly summarize `prefix'_p`k' if `touse' & `prefix'_status == 1, meanonly
            matrix `D'[`k', 3] = cond(r(N) > 0, r(mean), .)
            local rnames `"`rnames' opt`k'"'
        }
        matrix rownames `D' = `rnames'
    }

    // ---- 6. Display concise Stata summary -----------------------------------
    di as text _n "{hline 62}"
    di as text "decider (`qtype')  |  backend: " as result "`backend'" ///
        as text "  |  model: " as result "`model'"
    di as text "{hline 62}"
    di as text "  Observations in sample : " as result %9.0fc `N_total'
    di as text "  Scored                 : " as result %9.0fc `N_scored'
    di as text "  Missing text           : " as result %9.0fc `N_missing'
    di as text "  Flagged for review     : " as result %9.0fc `N_flagged' ///
        as text " (" as result %5.1f (100 * `share_flagged') as text "%)"
    if ("`qtype'" == "noul") {
        di as text "  Mean probability       : " as result %9.4f `mean_prob'
    }
    else {
        di as text "  Mean top probability   : " as result %9.4f `mean_top_p'
        di as text "  Mean margin            : " as result %9.4f `mean_margin'
        if (`rev_chk' == 1) {
            di as text "  Order-unstable rows    : " as result %9.0fc `N_unstable' ///
                as text " (" as result %5.1f (100 * `share_stable') as text "% stable)"
        }
    }
    if (`has_cost') {
        di as text "  Reported API cost (USD): " as result %9.4f `cost_usd'
    }
    di as text "  Prompt SHA-256         : " as result substr("`prompt_sha'", 1, 16) "..."
    di as text "  Run directory          : " as result `"`rundir'"'
    di as text "{hline 62}"

    // ---- 7. Populate r() returned results -----------------------------------
    return scalar N             = `N_total'
    return scalar N_scored      = `N_scored'
    return scalar N_missing     = `N_missing'
    return scalar N_flagged     = `N_flagged'
    return scalar share_flagged = `share_flagged'
    return scalar workers       = `workers'
    return scalar n_requests    = `n_requests'
    if (`has_cost') {
        return scalar cost_usd  = `cost_usd'
    }

    if ("`qtype'" == "noul") {
        return scalar mean_prob = `mean_prob'
        return scalar threshold = `threshold'
    }
    else {
        return scalar mean_top_p  = `mean_top_p'
        return scalar mean_margin = `mean_margin'
        return scalar min_prob    = `min_prob'
        return scalar min_margin  = `min_margin'
        if (`rev_chk' == 1) {
            return scalar N_unstable   = `N_unstable'
            return scalar share_stable = `share_stable'
        }
        return matrix dist = `D'
    }

    return local cmd            "decider"
    return local subcmd         "`subcmd'"
    return local varlist        "`varlist'"
    return local type           "`qtype'"
    return local prefix         "`prefix'"
    return local backend        "`backend'"
    return local model          "`model'"
    return local returned_model `"`ret_model'"'
    return local prompt_sha256  "`prompt_sha'"
    return local prompt_file    `"`prompt_file'"'
    return local run_dir        `"`rundir'"'
    return local created_vars   `"`created_vars'"'
end
