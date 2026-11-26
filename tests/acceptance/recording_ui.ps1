# Sourced by verify-workspace.ps1 with its own WinUI process and new output directory.
function Select-RecordingOption([string]$Id, [string]$Label, [switch]$Prefix) {
    $combo = Element $Id
    function Matches-Option($item) {
        if (!$item -or $item.Current.ProcessId -ne $app.Id -or !$item.Current.IsEnabled) { return $false }
        if ($Prefix) { return $item.Current.Name.StartsWith($Label) }
        return $item.Current.Name -eq $Label
    }
    $selection = $combo.GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern)
    foreach ($item in $selection.Current.GetSelection()) {
        if (Matches-Option $item) { return }
    }
    $scroll = $null
    if ($combo.TryGetCurrentPattern([Windows.Automation.ScrollItemPattern]::Pattern, [ref]$scroll)) { $scroll.ScrollIntoView() }
    $expand = $combo.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
    $condition = New-Object Windows.Automation.PropertyCondition($ae::ControlTypeProperty, [Windows.Automation.ControlType]::ListItem)
    $option = Wait-Until {
        if ($expand.Current.ExpandCollapseState -eq [Windows.Automation.ExpandCollapseState]::Collapsed) { $expand.Expand() }
        foreach ($item in $combo.FindAll($tree, $condition)) {
            if (Matches-Option $item) { return $item }
        }
    } "option $Label in $Id"
    Select-Element $option
    $null = Wait-Until {
        foreach ($item in $selection.Current.GetSelection()) { if (Matches-Option $item) { return $true } }
    } "selected $Label in $Id"
}

function Test-IsolatedRecording([string]$Fixture) {
    if ((Split-Path -Leaf $Fixture) -ne 'recording_edit_fixture.exe') { throw 'Expected the ordinary EDIT test fixture' }
    $py = (Resolve-Path -LiteralPath '.venv/Scripts/python.exe').Path
    $driver = (Resolve-Path -LiteralPath 'tests/acceptance/recording_ui_desktop.py').Path
    $initial = & $py -B -X utf8 $driver environment
    if ($LASTEXITCODE -ne 0) { throw 'Cannot establish the original desktop and display' }
    $context = $initial | ConvertFrom-Json
    $observations.originalDesktop = $context.desktop
    $observations.environment = $context.environment
    $observations.executableSHA256 = (Get-FileHash -LiteralPath $exe).Hash
    $observations.fixtureSHA256 = (Get-FileHash -LiteralPath $Fixture).Hash
    Select-Element (Element NavRun)
    $scopeLabels = [ordered]@{window='新启动应用的主窗口'; region='框选区域'; display='单台显示器'}
    foreach ($scope in $scopeLabels.Keys) {
        $observations.currentScope = $scope
        Select-RecordingOption IsolatedRecordingScope $scopeLabels[$scope]
        if ($scope -ne 'window') { Select-RecordingOption IsolatedRecordingDisplay $context.primary -Prefix }
        if ($scope -eq 'region') {
            Invoke-Element PickIsolatedRecordingRegion
            & $py -B -X utf8 $driver region $app.Id $hwnd.ToInt64() $context.desktop (Join-Path $folder.FullName 'selected-region.json')
            if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the owned selection overlay' }
        }
        Invoke-Element StartIsolatedRecording
        $dialog = Wait-Until { File-Dialog } 'application executable picker'
        $handle = [IntPtr]$dialog.Current.NativeWindowHandle
        $owner = $handle
        for ($i=0; $i -lt 10 -and $owner -ne $hwnd -and $owner -ne [IntPtr]::Zero; $i++) {
            $owner = [OwnedWindow]::GetWindow($owner,4)
        }
        if ($dialog.Current.ClassName -ne '#32770' -or $owner -ne $hwnd) { throw 'File picker is outside the selected WinUI window' }
        if (![OwnedWindow]::SetFilename($handle,$Fixture)) { throw 'Missing owned filename control' }
        if (![OwnedWindow]::PostMessage($handle,273,[IntPtr]1,[IntPtr]::Zero)) { throw 'Owned file picker did not accept the EXE' }

        $raw = Join-Path $folder.FullName "$scope-receiver.json"
        & $py -B -X utf8 $driver record $app.Id $hwnd.ToInt64() $context.desktop $raw $Fixture
        if ($LASTEXITCODE -ne 0) { throw "Failed the $scope isolated desktop interaction; see $raw" }
        $status = Wait-Text RecordingStatus '已结束.*2 个完整动作.*0 处中止'
        (Element RecordingDraft).GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
        $value = Wait-Until {
            $text = (Element RecordingActions).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value
            if ($text) { $text }
        } "$scope completed draft"
        $draft = ConvertFrom-Json -InputObject $value
        if ($draft.Count -ne 2 -or $draft[0].action.kind -ne 'click' -or $draft[0].action.point.scope -ne $scope -or
            $draft[1].action.kind -ne 'key_sequence' -or @($draft[1].action.events).Count -ne 2) {
            throw "Unexpected $scope recording draft with $value"
        }
        $edges = @($draft[1].action.events)
        if ($edges[0].key -ne 'a' -or $edges[0].down -ne $true -or $edges[1].key -ne 'a' -or $edges[1].down -ne $false) {
            throw "Draft and target disagree on $scope A with $value"
        }
        $draftPath = Join-Path $folder.FullName "$scope-draft.json"
        $value | Out-File -LiteralPath $draftPath -Encoding utf8
        $observations[$scope] = [ordered]@{status=$status; draftSHA256=(Get-FileHash -LiteralPath $draftPath).Hash; receiverSHA256=(Get-FileHash -LiteralPath $raw).Hash}
    }
    $observations.currentScope = 'complete'
}
