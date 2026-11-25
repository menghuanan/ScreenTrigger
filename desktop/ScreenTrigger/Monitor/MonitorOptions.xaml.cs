using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json;

namespace ScreenTrigger.Monitor;

public sealed partial class MonitorOptions : UserControl
{
    public MonitorOptions() { InitializeComponent(); Reset(); }

    internal void SetScripts(List<CatalogItem> scripts)
    {
        foreach (var box in new[] { ActionScript, TimeoutScript })
        {
            var selected = (box.SelectedItem as CatalogItem)?.Id;
            box.ItemsSource = scripts;
            box.SelectedItem = scripts.Find(row => row.Id == selected) ?? scripts.FirstOrDefault();
        }
    }
    internal void Reset()
    {
        Hit.SelectedIndex = TimeoutPolicy.SelectedIndex = Trigger.SelectedIndex = 0;
        Repeat.IsChecked = false;
        ActionArgs.Text = FailureArgs.Text = "{}";
        WaitTimeout.Value = 30000;
        RetryDelay.Value = Cooldown.Value = 1000;
        Retries.Value = 1;
        ShowFields();
    }
    private void Change(object sender, SelectionChangedEventArgs args) => ShowFields();
    private void ToggleRepeat(object sender, RoutedEventArgs args) => ShowFields();
    private void ShowFields()
    {
        if (FailureFields == null) return;
        HitFields.Visibility = Hit.SelectedIndex == 1 ? Visibility.Visible : Visibility.Collapsed;
        RepeatFields.Visibility = Repeat.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        RetryFields.Visibility = TimeoutPolicy.SelectedIndex == 1 ? Visibility.Visible : Visibility.Collapsed;
        FailureFields.Visibility = TimeoutPolicy.SelectedIndex == 2 ? Visibility.Visible : Visibility.Collapsed;
    }

    internal void Apply(Dictionary<string, object?> fields)
    {
        bool action = Hit.SelectedIndex == 1, failure = TimeoutPolicy.SelectedIndex == 2;
        fields["on_match"] = action ? "run_script" : "stop";
        fields["action_script_id"] = action ? (ActionScript.SelectedItem as CatalogItem)?.Id : null;
        fields["action_args"] = JsonSerializer.Deserialize<JsonElement>(action ? ActionArgs.Text : "{}");
        fields["repeat"] = action && Repeat.IsChecked == true;
        fields["trigger"] = Trigger.SelectedIndex == 0 ? "rising_edge" : "every_match";
        fields["cooldown_ms"] = Cooldown.Value;
        fields["wait_timeout_ms"] = WaitTimeout.Value;
        fields["on_timeout"] = TimeoutPolicy.SelectedIndex switch { 1 => "retry", 2 => "run_script", _ => "stop" };
        fields["max_retries"] = TimeoutPolicy.SelectedIndex == 1 ? Retries.Value : 0;
        fields["retry_delay_ms"] = RetryDelay.Value;
        fields["timeout_script_id"] = failure ? (TimeoutScript.SelectedItem as CatalogItem)?.Id : null;
        fields["timeout_args"] = JsonSerializer.Deserialize<JsonElement>(failure ? FailureArgs.Text : "{}");
    }
}
