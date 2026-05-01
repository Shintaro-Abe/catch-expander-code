import { describe, it, expect } from "vitest"
import { periodToRange, durationMs, fmtDuration, fmtRelative, fmtDatetime } from "./time"

describe("periodToRange", () => {
  it("returns from < to for 24h", () => {
    const { from, to } = periodToRange("24h")
    expect(from < to).toBe(true)
    expect(from).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$/)
    expect(to).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$/)
  })

  it("returns from < to for 7d", () => {
    const { from, to } = periodToRange("7d")
    expect(from < to).toBe(true)
  })

  it("returns from < to for 30d", () => {
    const { from, to } = periodToRange("30d")
    expect(from < to).toBe(true)
  })

  it("24h range is approximately 24 hours", () => {
    const { from, to } = periodToRange("24h")
    const diff = new Date(to).getTime() - new Date(from).getTime()
    expect(diff).toBeGreaterThanOrEqual(23 * 60 * 60 * 1000)
    expect(diff).toBeLessThanOrEqual(25 * 60 * 60 * 1000)
  })
})

describe("durationMs", () => {
  it("returns null when completedAt is missing", () => {
    expect(durationMs("2024-01-01T00:00:00.000Z")).toBeNull()
  })

  it("returns null when completedAt is undefined", () => {
    expect(durationMs("2024-01-01T00:00:00.000Z", undefined)).toBeNull()
  })

  it("returns positive number for valid pair", () => {
    const result = durationMs("2024-01-01T00:00:00.000Z", "2024-01-01T00:01:30.000Z")
    expect(result).toBe(90000)
  })

  it("returns 0 for same timestamps", () => {
    const result = durationMs("2024-01-01T00:00:00.000Z", "2024-01-01T00:00:00.000Z")
    expect(result).toBe(0)
  })
})

describe("fmtDuration", () => {
  it("returns '—' for null", () => {
    expect(fmtDuration(null)).toBe("—")
  })

  it("returns '0s' for 0", () => {
    expect(fmtDuration(0)).toBe("0s")
  })

  it("returns '5s' for 5000ms", () => {
    expect(fmtDuration(5000)).toBe("5s")
  })

  it("returns '1m 30s' for 90000ms", () => {
    expect(fmtDuration(90000)).toBe("1m 30s")
  })

  it("returns '1h 1m' for 3700000ms", () => {
    expect(fmtDuration(3700000)).toBe("1h 1m")
  })

  it("returns minutes-only format for exactly 2 minutes", () => {
    expect(fmtDuration(120000)).toBe("2m 0s")
  })

  it("returns hours format for exactly 1 hour", () => {
    expect(fmtDuration(3600000)).toBe("1h 0m")
  })
})

describe("fmtRelative", () => {
  it("returns '—' for null", () => {
    expect(fmtRelative(null)).toBe("—")
  })

  it("returns 'たった今' for a recent timestamp", () => {
    const now = new Date().toISOString()
    expect(fmtRelative(now)).toBe("たった今")
  })

  it("returns minutes ago for a timestamp 5 minutes ago", () => {
    const fiveMinAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString()
    expect(fmtRelative(fiveMinAgo)).toBe("5分前")
  })

  it("returns hours ago for a timestamp 2 hours ago", () => {
    const twoHrsAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString()
    expect(fmtRelative(twoHrsAgo)).toBe("2時間前")
  })

  it("returns days ago for a timestamp 3 days ago", () => {
    const threeDaysAgo = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString()
    expect(fmtRelative(threeDaysAgo)).toBe("3日前")
  })
})

describe("fmtDatetime", () => {
  it("returns '—' for null", () => {
    expect(fmtDatetime(null)).toBe("—")
  })

  it("returns a non-empty string for a valid ISO date", () => {
    const result = fmtDatetime("2024-06-15T10:30:00.000Z")
    expect(result).not.toBe("—")
    expect(result.length).toBeGreaterThan(0)
  })

  it("contains the year for a known date", () => {
    const result = fmtDatetime("2024-06-15T10:30:00.000Z")
    expect(result).toContain("2024")
  })
})
