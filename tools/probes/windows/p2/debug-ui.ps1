Navigate NavScripts
$row=Wait-For { Find-Item 'P2 调试' -Name } 'saved debugger fixture'
Select-Item $row
(Find-Item '打开脚本' -Name).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
Invoke-ItemButton OpenDebug
Set-Field BreakpointLine '2'
Invoke-ItemButton AddBreakpoint
Wait-For { Find-Item '第 2 行' -Name } 'installed breakpoint' | Out-Null
Invoke-ItemButton StartDebug
$seen.variables=Wait-For { $e=Find-Item DebugVariables; if ($e.Current.Name -match 'n.*2') {$e.Current.Name} } 'preline n'
Save-Tree 'paused'
Invoke-ItemButton StepOver
Wait-For { $e=Find-Item DebugVariables; if ($e.Current.Name -match 'table') {$e.Current.Name} } 'table after step'
Invoke-ItemButton ContinueDebug
$seen.debugResult=Wait-For { $e=Find-Item DebugStatus; if ($e.Current.Name -match '已完成') {$e.Current.Name} } 'debug completion'
$seen.editorFocus=Wait-For { $e=$ae::FocusedElement; if ($e.Current.AutomationId -eq 'ScriptSource') {$e.Current.AutomationId} } 'restored editor focus'
if ((Find-Item 'ContinueDebug') -and !(Find-Item 'ContinueDebug').Current.IsOffscreen) {throw 'live debug controls still visible'}
