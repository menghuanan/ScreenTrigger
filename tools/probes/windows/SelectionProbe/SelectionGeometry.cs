using Windows.Foundation;
using Windows.Graphics;
using Microsoft.UI.Windowing;

namespace SelectionProbe;

internal static class SelectionGeometry
{
    // Some SDK/OS combinations expose IVectorView.GetAt but not IIterable.First.
    public static IEnumerable<DisplayArea> Displays()
    {
        var areas = DisplayArea.FindAll();
        for (int i = 0; i < areas.Count; i++) yield return areas[i];
    }

    public static DisplayArea DefaultDisplay(RectInt32 window)
    {
        var cx = window.X + window.Width / 2;
        var cy = window.Y + window.Height / 2;
        return Displays().OrderByDescending(area =>
        {
            var r = area.OuterBounds;
            long w = Math.Max(0, Math.Min(window.X + window.Width, r.X + r.Width) - Math.Max(window.X, r.X));
            long h = Math.Max(0, Math.Min(window.Y + window.Height, r.Y + r.Height) - Math.Max(window.Y, r.Y));
            return w * h;
        }).ThenByDescending(area =>
        {
            var r = area.OuterBounds;
            return cx >= r.X && cx < r.X + r.Width && cy >= r.Y && cy < r.Y + r.Height;
        }).ThenByDescending(area => area.IsPrimary).First();
    }

    // Convert the two local DIP edges before adding the physical client origin (07 §1).
    public static RectInt32 FromDrag(Point a, Point b, PointInt32 origin, double scale, RectInt32 bounds)
    {
        int Edge(double dip, int offset, int lo, int hi, bool upper)
        {
            var physical = (upper ? Math.Ceiling(dip * scale) : Math.Floor(dip * scale)) + offset;
            return checked((int)Math.Clamp(physical, lo, hi));
        }

        var x = Edge(Math.Min(a.X, b.X), origin.X, bounds.X, bounds.X + bounds.Width, false);
        var y = Edge(Math.Min(a.Y, b.Y), origin.Y, bounds.Y, bounds.Y + bounds.Height, false);
        var right = Edge(Math.Max(a.X, b.X), origin.X, bounds.X, bounds.X + bounds.Width, true);
        var bottom = Edge(Math.Max(a.Y, b.Y), origin.Y, bounds.Y, bounds.Y + bounds.Height, true);
        return new(x, y, right - x, bottom - y);
    }

    public static bool CanConfirm(RectInt32 r) => r.Width >= 10 && r.Height >= 10;
}
