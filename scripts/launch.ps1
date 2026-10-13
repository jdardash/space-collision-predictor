<#
.SYNOPSIS
    Start the SDA server with the keys this app understands, then open the globe.

.DESCRIPTION
    Keys are kept in the Windows user environment, never in a file in this repo.
    A shell that was already open when a key was saved does not see it, so this
    script reads each one from the user store and hands it to the server itself.
    The user store is the source of truth: a corrected key takes effect in the
    same terminal, even if that terminal inherited the old value.

    Paid keys (Google Maps, Anthropic, OpenAI) are the only way this app can bill
    you, so they stay off unless you pass -AllowPaid. They are saved under private
    names (SDA_STORED_ANTHROPIC_API_KEY and so on) so other tools on this machine
    do not pick them up, and they are mapped to the well-known name only inside
    the server's environment. Without -AllowPaid, a paid key already present in
    the calling shell is removed for the server too.

    A local Ollama is picked up for the agent when it has a model that can call
    tools, and it answers first. Without -AllowPaid the server's provider list is
    always ollama, whatever the calling shell carries in SDA_LLM_PROVIDERS, and
    with no model pulled the assistant is simply unavailable. Key values are
    never printed, only names and
    whether each one is set. Every environment change the script makes is undone
    when it exits, so the calling shell is left exactly as it was.

    -WhatIf changes nothing: it stops no server, starts none, saves nothing and
    opens no browser. It prints the status table, the port it would bind, and the
    names (never values) of the variables it would hand to the server or withhold.

.PARAMETER SetKeys
    Ask for each key once (Enter keeps what is stored), check the ones that can
    be checked for free against their provider, save them, then launch.

.PARAMETER Status
    Show which keys are set and what each one unlocks, then exit.

.PARAMETER AllowPaid
    Hand the stored paid keys to the server. Without it a paid key never reaches
    the server, even if one is already set in the calling shell.

.PARAMETER Port
    Port to bind. Default: the first free port from 8010 to 8019. Port 8000 is
    used only when you pass -Port 8000, so a Docker container that has not
    started yet can still publish it. A server already running from this
    checkout is restarted in place.

.PARAMETER NoBrowser
    Do not open the globe in the default browser.

.EXAMPLE
    .\scripts\launch.ps1 -SetKeys

.EXAMPLE
    .\scripts\launch.ps1 -WhatIf -AllowPaid
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$SetKeys,
    [switch]$Status,
    [switch]$AllowPaid,
    [int]$Port = 0,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$OllamaUrl = 'http://127.0.0.1:11434'
# -WhatIf is honored through $DryRun. Leaving the preference on would make every
# cmdlet below echo "What if" lines, module auto-loading included.
$DryRun = [bool]$WhatIfPreference
$WhatIfPreference = $false

# Every credential the server reads, spelled exactly as the code reads it. Cost
# is what using the key can bill: PAID keys are the only way this app spends
# money, and it runs in full without any of them (NASA and Esri basemaps,
# keyless feeds, a local Ollama model for the agent).
$Keys = @(
    @{ Name = 'GOOGLE_MAPS_API_KEY';   Cost = 'PAID'; Secret = $true;  Unlocks = 'Google Photorealistic 3D Tiles on the globe';   From = 'console.cloud.google.com: needs billing, enable "Map Tiles API", then Credentials' }
    @{ Name = 'CESIUM_ION_TOKEN';      Cost = 'free'; Secret = $true;  Unlocks = 'Cesium World Terrain on the globe';             From = 'ion.cesium.com/tokens (free community account)' }
    @{ Name = 'ANTHROPIC_API_KEY';     Cost = 'PAID'; Secret = $true;  Unlocks = 'agent chat (POST /agent/chat) through Claude';  From = 'console.anthropic.com, billed per token' }
    @{ Name = 'OPENAI_API_KEY';        Cost = 'PAID'; Secret = $true;  Unlocks = 'agent chat through an OpenAI-compatible API';   From = 'platform.openai.com, OpenRouter or Groq, billed per token' }
    @{ Name = 'FIRMS_MAP_KEY';         Cost = 'free'; Secret = $true;  Unlocks = 'NASA fire detections feed';                     From = 'firms.modaps.eosdis.nasa.gov/api/map_key' }
    @{ Name = 'AISSTREAM_API_KEY';     Cost = 'free'; Secret = $true;  Unlocks = 'ship positions feed';                           From = 'aisstream.io' }
    @{ Name = 'OPENSKY_CLIENT_ID';     Cost = 'free'; Secret = $false; Unlocks = 'authenticated OpenSky flights (higher limits)'; From = 'opensky-network.org account, API client' }
    @{ Name = 'OPENSKY_CLIENT_SECRET'; Cost = 'free'; Secret = $true;  Unlocks = 'pairs with OPENSKY_CLIENT_ID';                  From = 'same page as the client id' }
    @{ Name = 'SDA_SPACETRACK_USER';   Cost = 'free'; Secret = $false; Unlocks = 'Space-Track GP, TIP and CDM pulls';             From = 'space-track.org account' }
    @{ Name = 'SDA_SPACETRACK_PASS';   Cost = 'free'; Secret = $true;  Unlocks = 'pairs with SDA_SPACETRACK_USER';                From = 'space-track.org account' }
    @{ Name = 'SDA_WEBHOOK_URLS';      Cost = 'free'; Secret = $true;  Unlocks = 'alert delivery to Slack, Discord or a webhook'; From = 'your chat tool, comma separated' }
)

# Every process-environment change goes through Set-ProcessEnv so the previous
# value is on record and Restore-ProcessEnv can put the calling shell back.
$script:EnvPrev = [ordered]@{}
$script:EnvHanded = New-Object System.Collections.Generic.List[string]
$script:EnvRemoved = New-Object System.Collections.Generic.List[string]

function Write-ProcessEnv([string]$Name, [string]$Value) {
    # The Env: provider, not [Environment]: in PowerShell 7 a variable removed
    # through the .NET call lingers as an empty name that child processes still see.
    if ($Value) {
        Set-Item -Path "Env:$Name" -Value $Value
    } else {
        Remove-Item -Path "Env:$Name" -ErrorAction SilentlyContinue
    }
}

function Set-ProcessEnv([string]$Name, [string]$Value) {
    # An empty or null value removes the variable.
    $previous = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not $script:EnvPrev.Contains($Name)) { $script:EnvPrev[$Name] = $previous }
    Write-ProcessEnv $Name $Value
    if ($Value) {
        $script:EnvHanded.Add($Name)
    } elseif ($previous) {
        $script:EnvRemoved.Add($Name)
    }
}

function Restore-ProcessEnv {
    foreach ($name in @($script:EnvPrev.Keys)) { Write-ProcessEnv $name $script:EnvPrev[$name] }
    $script:EnvPrev.Clear()
}

function Get-StoredValue([string]$Name) {
    # The user store is the source of truth, so a corrected key wins in a terminal
    # that inherited the old one. A variable only this shell has is the fallback.
    foreach ($scope in 'User', 'Process') {
        $value = [Environment]::GetEnvironmentVariable($Name, $scope)
        if (-not [string]::IsNullOrWhiteSpace($value)) { return $value.Trim() }
    }
    return $null
}

function Get-StoreName($Key) {
    # A paid key is saved under a private name so other tools on this machine do
    # not find it under the name they read and start billing it.
    if ($Key.Cost -eq 'PAID') { return "SDA_STORED_$($Key.Name)" }
    return $Key.Name
}

function Resolve-Keys {
    # One pass over the stores, before the launcher touches the process
    # environment: what is stored, and what this run would hand to the server.
    foreach ($key in $Keys) {
        $item = [pscustomobject]@{
            Name = $key.Name; Cost = $key.Cost; Unlocks = $key.Unlocks
            Stored = $false; Ambient = $false; Present = $false; Withheld = $false; Value = $null
        }
        if ($key.Cost -eq 'PAID') {
            $stored = Get-StoredValue (Get-StoreName $key)
            $ambient = Get-StoredValue $key.Name
            $item.Stored = [bool]$stored
            $item.Ambient = [bool]$ambient
            $item.Present = [bool]($stored -or $ambient)
            if ($AllowPaid) {
                $item.Value = if ($stored) { $stored } else { $ambient }
            } else {
                $item.Withheld = $item.Present
            }
        } else {
            $item.Value = Get-StoredValue $key.Name
            $item.Present = [bool]$item.Value
        }
        $item
    }
}

function Test-Key([string]$Name, [string]$Value) {
    # 'ok', 'unchecked' (no free check exists) or a one-line reason. The key
    # itself never appears in what is returned. GOOGLE_MAPS_API_KEY has no free
    # check: every endpoint that accepts it is a billable request.
    $request = $null
    switch ($Name) {
        'CESIUM_ION_TOKEN'    { $request = @{ Uri = "https://api.cesium.com/v1/assets/1/endpoint?access_token=$Value" } }
        'ANTHROPIC_API_KEY'   { $request = @{ Uri = 'https://api.anthropic.com/v1/models'; Headers = @{ 'x-api-key' = $Value; 'anthropic-version' = '2023-06-01' } } }
    }
    if (-not $request) { return 'unchecked' }
    try {
        Invoke-WebRequest @request -UseBasicParsing -TimeoutSec 20 | Out-Null
        return 'ok'
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        if ($code) { return "the provider answered HTTP $code" }
        return 'the provider could not be reached'
    }
}

function Find-OllamaModel([string]$BaseUrl) {
    # The agent drives typed tools, so only models that can call tools qualify.
    try {
        $tags = Invoke-RestMethod -Uri "$BaseUrl/api/tags" -TimeoutSec 3
    } catch {
        return $null
    }
    $names = @($tags.models | ForEach-Object { $_.name })
    foreach ($prefix in 'llama3.1', 'qwen2.5') {
        $hit = $names | Where-Object { $_ -like "$prefix*" } | Select-Object -First 1
        if ($hit) { return $hit }
    }
    return $null
}

function Get-AgentModel {
    # Asked once per run, against the Ollama the user configured or the default.
    if (-not $script:ModelChecked) {
        $script:ModelChecked = $true
        $base = if ($env:SDA_OLLAMA_URL) { $env:SDA_OLLAMA_URL.TrimEnd('/') } else { $OllamaUrl }
        $script:AgentModel = Find-OllamaModel $base
    }
    return $script:AgentModel
}

function Show-Status($Resolved) {
    foreach ($item in $Resolved) {
        $state = if ($item.Present) { 'set' } else { '-' }
        $note = ''
        if ($item.Cost -eq 'PAID') {
            $where = if ($item.Stored) { 'stored' } elseif ($item.Ambient) { 'own name only' } else { 'not stored' }
            $fate = if (-not $item.Present) { '' } elseif ($item.Withheld) { '; withheld' } else { '; handed over' }
            $note = "  [$where$fate]"
        }
        Write-Host ('{0,-22} {1,-4} {2,-5} {3}{4}' -f $item.Name, $state, $item.Cost, $item.Unlocks, $note)
    }
    $model = Get-AgentModel
    $ollama = if ($model) { "running, agent will use $model" } else { 'not running, or no tool-calling model pulled' }
    Write-Host ('{0,-22} {1,-4} {2,-5} {3}' -f 'local Ollama', '', 'free', $ollama)

    $paidAgent = @($Resolved | Where-Object { $_.Value -and ($_.Name -eq 'ANTHROPIC_API_KEY' -or $_.Name -eq 'OPENAI_API_KEY') })
    if (-not $model -and -not $paidAgent) {
        Write-Host 'Agent chat is off. Turn it on with: ollama pull llama3.1  (or save a paid key with -SetKeys and pass -AllowPaid)'
    }
    $withheld = @($Resolved | Where-Object { $_.Withheld } | ForEach-Object { $_.Name })
    if ($withheld) {
        Write-Host "Paid keys stored or present but withheld from the server: $($withheld -join ', '). -AllowPaid turns them on."
    }
    $canBill = @($Resolved | Where-Object { $_.Cost -eq 'PAID' -and $_.Value } | ForEach-Object { $_.Name })
    if ($canBill) {
        Write-Host "Can bill on this run: $($canBill -join ', ')"
    } else {
        Write-Host 'Nothing set here can bill you.'
    }
}

function Read-Keys {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-Host 'Enter each key, or press Enter to keep what is stored. Secret values are not echoed.'
    foreach ($key in $Keys) {
        $store = Get-StoreName $key
        $current = if ($key.Cost -eq 'PAID') {
            if (Get-StoredValue $store) { 'stored' }
            elseif (Get-StoredValue $key.Name) { 'not stored, but present under its own name' }
            else { 'not stored' }
        } else {
            if (Get-StoredValue $store) { 'set' } else { 'not set' }
        }
        Write-Host ''
        Write-Host "$($key.Name) ($current, $($key.Cost)): $($key.Unlocks)"
        Write-Host "  from: $($key.From)"
        if ($key.Cost -eq 'PAID') { Write-Host "  saved as $store, and handed to the server only with -AllowPaid" }
        if ($key.Secret) {
            $secure = Read-Host '  value' -AsSecureString
            $value = (New-Object System.Net.NetworkCredential('', $secure)).Password
        } else {
            $value = Read-Host '  value'
        }
        $value = "$value".Trim()
        if (-not $value) { continue }
        $result = Test-Key $key.Name $value
        if (-not $PSCmdlet.ShouldProcess($store, 'Save to the user environment')) {
            Write-Host '  not saved'
            continue
        }
        [Environment]::SetEnvironmentVariable($store, $value, 'User')
        switch ($result) {
            'ok'        { Write-Host '  saved, accepted by the provider' }
            'unchecked' { Write-Host '  saved (no free check exists for this one)' }
            default     { Write-Host "  saved, but $result" }
        }
    }
    Write-Host ''
}

function Get-Listener([int]$LocalPort) {
    $conn = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    $process = $null
    try { $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction Stop } catch { }
    if ($process) { return $process }
    # Something listens but its process does not resolve (it just exited, or it is
    # not ours to read). The port is taken, and the listener is not our server.
    return [pscustomobject]@{
        ProcessId = $conn.OwningProcess; ParentProcessId = -1; CommandLine = $null; Unresolved = $true
        Name = "an unidentified process (PID $($conn.OwningProcess))"
    }
}

function Test-OwnServer($Process) {
    # Only a server started from this checkout is ours to stop.
    if (-not $Process -or $Process.Unresolved -or -not $Process.CommandLine) { return $false }
    $fromHere = $Process.CommandLine.IndexOf($Python, [StringComparison]::OrdinalIgnoreCase) -ge 0
    return $fromHere -and $Process.CommandLine -match '-m\s+sda\.api'
}

function Find-Port {
    # 8000 is skipped on purpose: Docker Desktop may not have published it yet.
    $candidates = if ($Port) { @($Port) } else { 8010..8019 }
    foreach ($candidate in $candidates) {
        $owner = Get-Listener $candidate
        if (Test-OwnServer $owner) { return [pscustomobject]@{ Port = $candidate; Replace = $owner } }
    }
    foreach ($candidate in $candidates) {
        $owner = Get-Listener $candidate
        if (-not $owner) { return [pscustomobject]@{ Port = $candidate; Replace = $null } }
        Write-Host "Port $candidate belongs to $($owner.Name); leaving it alone"
    }
    throw "No free port among: $($candidates -join ', ')"
}

$resolved = @(Resolve-Keys)
if ($Status) { Show-Status $resolved; return }
if (-not (Test-Path $Python)) { throw "No virtualenv at $Python. Create it and run: pip install -e ." }
if ($SetKeys) {
    if ($DryRun) {
        Write-Host 'WhatIf: -SetKeys would ask for each key and save it to the user environment; skipped.'
    } else {
        Read-Keys -WhatIf:$DryRun
        $resolved = @(Resolve-Keys)
    }
}

try {
    Show-Status $resolved

    foreach ($item in $resolved) {
        if ($item.Cost -eq 'PAID') {
            # The server never reads the private copy, so it does not get one.
            Set-ProcessEnv "SDA_STORED_$($item.Name)" $null
            # Without -AllowPaid this clears a key the shell already carried.
            if (-not $item.Value) { Set-ProcessEnv $item.Name $null }
        }
        if ($item.Value) { Set-ProcessEnv $item.Name $item.Value }
    }

    $model = Get-AgentModel
    if ($model) {
        if (-not $env:SDA_OLLAMA_URL) { Set-ProcessEnv 'SDA_OLLAMA_URL' $OllamaUrl }
        if (-not $env:SDA_OLLAMA_MODEL) { Set-ProcessEnv 'SDA_OLLAMA_MODEL' $model }
    }
    if ($AllowPaid) {
        if ($model) {
            if ($env:SDA_LLM_PROVIDERS) {
                Write-Host "SDA_LLM_PROVIDERS is already set to $($env:SDA_LLM_PROVIDERS); leaving it"
            } else {
                Set-ProcessEnv 'SDA_LLM_PROVIDERS' 'ollama,anthropic,openai'
                Write-Host 'Agent providers, in order: ollama,anthropic,openai'
            }
        }
    } else {
        # No paid key reaches the server without -AllowPaid, so it must not be told
        # to try one either. The server skips a provider that is cooling down and
        # falls through to the next, and a list inherited from the calling shell
        # (for example anthropic,openai,ollama) names providers it cannot use. The
        # list is forced even when no model was found, so it never carries a name
        # the run cannot honour.
        if ($env:SDA_LLM_PROVIDERS -and $env:SDA_LLM_PROVIDERS -ne 'ollama') {
            Write-Host 'SDA_LLM_PROVIDERS from the calling shell is replaced with ollama for this run'
        }
        Set-ProcessEnv 'SDA_LLM_PROVIDERS' 'ollama'
        if ($model) {
            Write-Host 'Agent providers, in order: ollama'
        } else {
            Write-Host 'No Ollama model with tool calling was found, so the assistant will be unavailable until one is pulled (ollama pull llama3.1).'
        }
    }
    # CelesTrak refuses a second download of a group inside its two-hour update
    # window, so without the last good catalog on disk a restart shrinks the globe
    # to the small bundled set.
    if (-not $env:SDA_TLE_CACHE_DIR) { Set-ProcessEnv 'SDA_TLE_CACHE_DIR' (Join-Path $env:LOCALAPPDATA 'sda\tle-cache') }

    $choice = Find-Port
    $boundPort = $choice.Port
    Set-ProcessEnv 'SDA_PORT' "$boundPort"
    $url = "http://127.0.0.1:$boundPort"

    if ($DryRun) {
        Write-Host 'WhatIf: nothing is stopped, started, saved or opened.'
        if ($choice.Replace) {
            Write-Host "  Port $boundPort : a server from this checkout runs there (PID $($choice.Replace.ProcessId)) and would be restarted"
        } else {
            Write-Host "  Port $boundPort : free, a new server would bind it"
        }
        Write-Host "  Variables handed to the server: $($script:EnvHanded -join ', ')"
        $removed = if ($script:EnvRemoved.Count) { $script:EnvRemoved -join ', ' } else { 'none' }
        Write-Host "  Variables removed from its environment: $removed"
        $withheld = @($resolved | Where-Object { $_.Withheld } | ForEach-Object { $_.Name })
        $withheldText = if ($withheld) { "$($withheld -join ', ') (-AllowPaid turns them on)" } else { 'none' }
        Write-Host "  Paid keys withheld: $withheldText"
        return
    }

    if ($choice.Replace) {
        Write-Host "Restarting the server already on port $boundPort (PID $($choice.Replace.ProcessId))"
        Stop-Process -Id $choice.Replace.ProcessId -Force
        for ($i = 0; $i -lt 40 -and (Get-Listener $boundPort); $i++) { Start-Sleep -Milliseconds 250 }
    }

    $server = Start-Process -FilePath $Python -ArgumentList '-m', 'sda.api' -WorkingDirectory $RepoRoot -NoNewWindow -PassThru
    try {
        $ready = $false
        for ($i = 0; $i -lt 60 -and -not $server.HasExited; $i++) {
            try {
                Invoke-WebRequest -Uri "$url/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
                $ready = $true
                break
            } catch {
                Start-Sleep -Seconds 1
            }
        }
        if (-not $ready) { throw "The server did not answer on $url/health" }
        Write-Host "Globe: $url/worldview   (Ctrl+C here stops the server)"
        if (-not $NoBrowser) { Start-Process "$url/worldview" }
        $server.WaitForExit()
    } finally {
        if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
        # The venv python.exe is a shim that runs the real interpreter as a child,
        # so the listener can outlive it. Stop it only when it is that child: a
        # second launcher may already have restarted the port with its own server.
        $left = Get-Listener $boundPort
        if ($left -and -not $left.Unresolved -and $left.ParentProcessId -eq $server.Id) { Stop-Process -Id $left.ProcessId -Force }
    }
} finally {
    Restore-ProcessEnv
}
