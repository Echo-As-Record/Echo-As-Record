import CryptoKit
import Foundation
import zlib

struct RecoveredGzip {
  let lineCount: Int
}

final class GzipRecovery {
  private let fileManager: FileManager

  init(fileManager: FileManager = .default) {
    self.fileManager = fileManager
  }

  func recover(
    _ url: URL,
    onJsonLine: ((Any) -> Bool)? = nil
  ) -> RecoveredGzip? {
    let temporaryURL = url.appendingPathExtension("recovering")
    try? fileManager.removeItem(at: temporaryURL)
    guard let outputFile = gzopen(temporaryURL.path, "wb") else {
      return nil
    }
    var outputWriteFailed = false
    let source = scan(
      url,
      onJsonLine: onJsonLine,
      onCompleteLine: { line in
        guard !outputWriteFailed else { return }
        if !self.write(line, to: outputFile) ||
            !self.write(Self.newline, to: outputFile) {
          outputWriteFailed = true
        }
      }
    )
    let closeResult = gzclose(outputFile)
    guard let source else {
      try? fileManager.removeItem(at: temporaryURL)
      return nil
    }
    if source.complete {
      try? fileManager.removeItem(at: temporaryURL)
      return RecoveredGzip(lineCount: source.lineCount)
    }
    guard !outputWriteFailed, closeResult == Z_OK else {
      try? fileManager.removeItem(at: temporaryURL)
      return nil
    }
    do {
      let handle = try FileHandle(forWritingTo: temporaryURL)
      try handle.synchronize()
      try handle.close()
    } catch {
      try? fileManager.removeItem(at: temporaryURL)
      return nil
    }
    guard
      let validation = scan(temporaryURL),
      validation.complete,
      validation.lineCount == source.lineCount,
      validation.payloadByteCount == source.payloadByteCount,
      validation.payloadDigest == source.payloadDigest
    else {
      try? fileManager.removeItem(at: temporaryURL)
      return nil
    }
    do {
      _ = try fileManager.replaceItemAt(url, withItemAt: temporaryURL)
      return RecoveredGzip(lineCount: source.lineCount)
    } catch {
      try? fileManager.removeItem(at: temporaryURL)
      return nil
    }
  }

  private func scan(
    _ url: URL,
    onJsonLine: ((Any) -> Bool)? = nil,
    onCompleteLine: ((Data) -> Void)? = nil
  ) -> GzipJsonlScan? {
    guard let inputFile = gzopen(url.path, "rb") else {
      return nil
    }
    var hasher = SHA256()
    var lineBuffer = Data()
    var readBuffer = [UInt8](repeating: 0, count: Self.bufferSize)
    var payloadByteCount = 0
    var lineCount = 0
    var terminalReadCount: Int32 = 0
    while true {
      let readCount = readBuffer.withUnsafeMutableBytes { rawBuffer -> Int32 in
        guard let baseAddress = rawBuffer.baseAddress else { return 0 }
        return gzread(inputFile, baseAddress, UInt32(rawBuffer.count))
      }
      guard readCount > 0 else {
        terminalReadCount = readCount
        break
      }
      var offset = 0
      for index in 0..<Int(readCount) where readBuffer[index] == Self.newlineByte {
        let lineChunkCount = index - offset
        guard lineBuffer.count <= Self.maxJsonlLineBytes - lineChunkCount else {
          gzclose(inputFile)
          return nil
        }
        lineBuffer.append(contentsOf: readBuffer[offset..<index])
        let line = lineBuffer
        lineBuffer.removeAll(keepingCapacity: true)
        offset = index + 1
        hasher.update(data: line)
        hasher.update(data: Self.newline)
        payloadByteCount += line.count + 1
        if !line.isEmpty {
          guard
            String(data: line, encoding: .utf8) != nil,
            let json = try? JSONSerialization.jsonObject(with: line),
            onJsonLine?(json) != false
          else {
            gzclose(inputFile)
            return nil
          }
          lineCount += 1
        }
        onCompleteLine?(line)
      }
      if offset < readCount {
        let tailCount = Int(readCount) - offset
        guard lineBuffer.count <= Self.maxJsonlLineBytes - tailCount else {
          gzclose(inputFile)
          return nil
        }
        lineBuffer.append(contentsOf: readBuffer[offset..<Int(readCount)])
      }
    }
    var errorCode: Int32 = Z_OK
    _ = gzerror(inputFile, &errorCode)
    let complete = terminalReadCount == 0 && errorCode == Z_OK && gzeof(inputFile) == 1
    gzclose(inputFile)
    guard lineCount > 0 else {
      return nil
    }
    return GzipJsonlScan(
      lineCount: lineCount,
      payloadByteCount: payloadByteCount,
      payloadDigest: Array(hasher.finalize()),
      complete: complete
    )
  }

  private func write(_ data: Data, to gzipFile: gzFile) -> Bool {
    if data.isEmpty {
      return true
    }
    return data.withUnsafeBytes { rawBuffer in
      guard let baseAddress = rawBuffer.baseAddress else { return false }
      var offset = 0
      while offset < rawBuffer.count {
        let count = min(Self.bufferSize, rawBuffer.count - offset)
        let written = gzwrite(
          gzipFile,
          baseAddress.advanced(by: offset),
          UInt32(count)
        )
        guard written == count else { return false }
        offset += count
      }
      return true
    }
  }

  private struct GzipJsonlScan {
    let lineCount: Int
    let payloadByteCount: Int
    let payloadDigest: [UInt8]
    let complete: Bool
  }

  private static let bufferSize = 64 * 1024
  private static let maxJsonlLineBytes = 1024 * 1024
  private static let newlineByte: UInt8 = 10
  private static let newline = Data([newlineByte])
}
