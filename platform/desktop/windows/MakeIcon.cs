// 现画图标，不往仓库里塞二进制 —— 和 mac 那边 `MakeIcon.swift` 同一份图、同一个理由。
//
// 画的是一个刻度盘：一段圆弧 + 一根指针。选它的理由是这套东西干的事 ——
// 把一堆过程读数收敛成一个可以拿去用的结论。不用烧杯、不用大脑、不用星星。
//
// ## 为什么 Windows 也得有
//
// 2026-09-21 wangd 要的是「桌面快捷方式，或者开始菜单栏快捷方式」。快捷方式的全部
// 意义就是**一眼认出来**；`csc` 不给 `/win32icon` 编出来的 exe 顶着 .NET 默认图标，
// 桌面上摆一个那个，和 mac 那边说的「Dock 里是一张白纸，看起来像装坏了」是同一件事。
//
// ## 为什么自己写 .ico
//
// 构建机没有管理员权限，装不了任何图标工具；`System.Drawing` 只会存单张 PNG/BMP，
// 存不出多尺寸 ICO 容器。容器格式本身很小，直接按 spec 拼：
// 16/32/48 走 32bpp BMP（Explorer 与任务栏最常取这三档，最老的取图路径也认），
// 256 走 PNG（BMP 到这个尺寸会把文件撑大一个数量级，Vista 起就是这么存的）。
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

internal static class MakeIcon
{
    private static readonly int[] Sizes = { 16, 32, 48, 256 };

    /// <summary>GDI+ 的角度是顺时针、y 向下；mac 那份是逆时针、y 向上。这里是镜像过的同一张图。</summary>
    private const float Start = -207f;   // 盘口起点
    private const float Sweep = 234f;    // 整圈开口
    private const float Walked = 0.62f;  // 走过的比例：一个正在进行、还没走完的过程

    private static Bitmap Draw(int size)
    {
        var bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);

            float inset = size * 0.06f;
            float side = size - inset * 2;
            float radius = size * 0.2237f;          // Big Sur 的圆角比例，两边一致
            using (var body = RoundedRect(inset, inset, side, side, radius))
            using (var ground = new LinearGradientBrush(
                       new RectangleF(0, 0, size, size),
                       Color.FromArgb(255, 23, 33, 61), Color.FromArgb(255, 13, 18, 36),
                       LinearGradientMode.Vertical))
                g.FillPath(ground, body);

            float cx = size / 2f, cy = size * 0.54f;
            float dial = size * 0.27f;
            float width = Math.Max(1f, size * 0.055f);
            var box = new RectangleF(cx - dial, cy - dial, dial * 2, dial * 2);

            using (var track = new Pen(Color.FromArgb(41, 255, 255, 255), width))
            {
                track.StartCap = track.EndCap = LineCap.Round;
                g.DrawArc(track, box, Start, Sweep);
            }
            using (var walked = new Pen(Color.FromArgb(255, 97, 209, 222), width))
            {
                walked.StartCap = walked.EndCap = LineCap.Round;
                g.DrawArc(walked, box, Start, Sweep * Walked);
            }

            double angle = (Start + Sweep * Walked) * Math.PI / 180.0;
            using (var needle = new Pen(Color.White, width * 0.75f))
            {
                needle.StartCap = needle.EndCap = LineCap.Round;
                g.DrawLine(needle, cx, cy,
                           (float)(cx + Math.Cos(angle) * dial * 0.72),
                           (float)(cy + Math.Sin(angle) * dial * 0.72));
            }

            float hub = size * 0.035f;
            g.FillEllipse(Brushes.White, cx - hub, cy - hub, hub * 2, hub * 2);
        }
        return bmp;
    }

    private static GraphicsPath RoundedRect(float x, float y, float w, float h, float r)
    {
        float d = r * 2;
        var path = new GraphicsPath();
        path.AddArc(x, y, d, d, 180, 90);
        path.AddArc(x + w - d, y, d, d, 270, 90);
        path.AddArc(x + w - d, y + h - d, d, d, 0, 90);
        path.AddArc(x, y + h - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    /// <summary>一张 32bpp BMP 的 ICO 成员：BITMAPINFOHEADER（高度写两倍）+ BGRA 自下而上 + 空 AND 掩码。</summary>
    private static byte[] AsDib(Bitmap bmp)
    {
        int size = bmp.Width;
        var body = new MemoryStream();
        var w = new BinaryWriter(body);
        w.Write(40); w.Write(size); w.Write(size * 2);
        w.Write((short)1); w.Write((short)32);
        w.Write(0); w.Write(size * size * 4);
        w.Write(0); w.Write(0); w.Write(0); w.Write(0);
        for (int y = size - 1; y >= 0; y--)
            for (int x = 0; x < size; x++)
            {
                Color c = bmp.GetPixel(x, y);
                w.Write(c.B); w.Write(c.G); w.Write(c.R); w.Write(c.A);
            }
        int maskRow = ((size + 31) / 32) * 4;      // 1bpp，行按 4 字节对齐
        w.Write(new byte[maskRow * size]);         // 全 0：透明度由 alpha 说了算
        w.Flush();
        return body.ToArray();
    }

    private static byte[] AsPng(Bitmap bmp)
    {
        var png = new MemoryStream();
        bmp.Save(png, ImageFormat.Png);
        return png.ToArray();
    }

    private static int Main(string[] args)
    {
        string output = args.Length > 0 ? args[0] : "ScienceMate.ico";
        var members = new List<KeyValuePair<int, byte[]>>();
        foreach (int size in Sizes)
            using (Bitmap bmp = Draw(size))
                members.Add(new KeyValuePair<int, byte[]>(size, size >= 256 ? AsPng(bmp) : AsDib(bmp)));

        using (var file = new FileStream(output, FileMode.Create, FileAccess.Write))
        using (var w = new BinaryWriter(file))
        {
            w.Write((short)0); w.Write((short)1); w.Write((short)members.Count);
            int offset = 6 + 16 * members.Count;
            foreach (var m in members)
            {
                w.Write((byte)(m.Key >= 256 ? 0 : m.Key));   // 256 在 ICO 里写 0
                w.Write((byte)(m.Key >= 256 ? 0 : m.Key));
                w.Write((byte)0); w.Write((byte)0);
                w.Write((short)1); w.Write((short)32);
                w.Write(m.Value.Length); w.Write(offset);
                offset += m.Value.Length;
            }
            foreach (var m in members) w.Write(m.Value);
        }
        Console.WriteLine("  画了 " + members.Count + " 档尺寸 → " + output);
        return 0;
    }
}
