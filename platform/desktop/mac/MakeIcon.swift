// 现画图标，不往仓库里塞二进制。
//
// 画的是一个刻度盘：一段圆弧 + 一根指针。选它的理由是这套东西干的事 ——
// 把一堆过程读数收敛成一个可以拿去用的结论。不用烧杯、不用大脑、不用星星。

import AppKit

let sizes = [16, 32, 64, 128, 256, 512, 1024]

func draw(size: CGFloat) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let context = NSGraphicsContext.current!.cgContext

    // 底：macOS 图标的圆角矩形，深墨蓝。
    let inset = size * 0.06
    let body = CGRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)
    let radius = size * 0.2237  // Big Sur 的圆角比例
    let ground = CGPath(roundedRect: body, cornerWidth: radius, cornerHeight: radius,
                        transform: nil)
    context.addPath(ground)
    context.clip()
    let gradient = CGGradient(
        colorsSpace: CGColorSpaceCreateDeviceRGB(),
        colors: [NSColor(calibratedRed: 0.09, green: 0.13, blue: 0.24, alpha: 1).cgColor,
                 NSColor(calibratedRed: 0.05, green: 0.07, blue: 0.14, alpha: 1).cgColor] as CFArray,
        locations: [0, 1])!
    context.drawLinearGradient(gradient, start: CGPoint(x: 0, y: size),
                               end: CGPoint(x: 0, y: 0), options: [])

    let center = CGPoint(x: size / 2, y: size * 0.46)
    let dialRadius = size * 0.27
    let lineWidth = max(1, size * 0.055)

    // 刻度盘底弧：暗的一整圈开口。
    context.setLineCap(.round)
    context.setLineWidth(lineWidth)
    context.setStrokeColor(NSColor(calibratedWhite: 1, alpha: 0.16).cgColor)
    context.addArc(center: center, radius: dialRadius,
                   startAngle: .pi * 1.15, endAngle: .pi * -0.15, clockwise: true)
    context.strokePath()

    // 走过的那一段：亮青。停在 0.62 —— 一个正在进行、还没走完的过程。
    context.setStrokeColor(NSColor(calibratedRed: 0.38, green: 0.82, blue: 0.87, alpha: 1).cgColor)
    let sweep = CGFloat.pi * 1.3
    context.addArc(center: center, radius: dialRadius,
                   startAngle: .pi * 1.15,
                   endAngle: .pi * 1.15 - sweep * 0.62, clockwise: true)
    context.strokePath()

    // 指针：指着走到的地方。
    let angle = CGFloat.pi * 1.15 - sweep * 0.62
    context.setLineWidth(lineWidth * 0.75)
    context.setStrokeColor(NSColor.white.cgColor)
    context.move(to: center)
    context.addLine(to: CGPoint(x: center.x + cos(angle) * dialRadius * 0.72,
                                y: center.y + sin(angle) * dialRadius * 0.72))
    context.strokePath()

    // 轴心。
    context.setFillColor(NSColor.white.cgColor)
    let hub = size * 0.035
    context.fillEllipse(in: CGRect(x: center.x - hub, y: center.y - hub,
                                   width: hub * 2, height: hub * 2))

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

let directory = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "."
for size in sizes {
    for (scale, suffix) in [(1, ""), (2, "@2x")] {
        let pixels = size * scale
        if pixels > 1024 { continue }
        let rep = draw(size: CGFloat(pixels))
        guard let data = rep.representation(using: .png, properties: [:]) else { continue }
        let name = "icon_\(size)x\(size)\(suffix).png"
        try! data.write(to: URL(fileURLWithPath: directory).appendingPathComponent(name))
    }
}
print("  画了 \(sizes.count) 档尺寸")
