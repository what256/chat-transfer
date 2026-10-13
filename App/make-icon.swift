import AppKit
import Foundation
let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
for (name, size) in [("icon_16x16.png",16),("icon_16x16@2x.png",32),("icon_32x32.png",32),
                     ("icon_32x32@2x.png",64),("icon_128x128.png",128),("icon_128x128@2x.png",256),
                     ("icon_256x256.png",256),("icon_256x256@2x.png",512),
                     ("icon_512x512.png",512),("icon_512x512@2x.png",1024)] {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                  colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let length = CGFloat(size)
    NSColor(calibratedWhite: 0.17, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: length * 0.04, y: length * 0.04,
                                   width: length * 0.92, height: length * 0.92),
                 xRadius: length * 0.21, yRadius: length * 0.21).fill()
    let image = NSImage(systemSymbolName: "arrow.left.arrow.right", accessibilityDescription: nil)!
        .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: length * 0.5, weight: .medium)
                                  .applying(NSImage.SymbolConfiguration(hierarchicalColor: .white)))!
    image.draw(in: NSRect(x: length * 0.22, y: length * 0.22, width: length * 0.56, height: length * 0.56))
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using: .png, properties: [:])!.write(to: folder.appendingPathComponent(name))
}
