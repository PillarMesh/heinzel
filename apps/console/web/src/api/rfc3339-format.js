// RFC 3339 date-time validation, shared by the browser bundle and the
// precompiled validators.
//
// Plain JavaScript on purpose: Ajv's standalone output imports this module
// directly at runtime, so it cannot be TypeScript that only exists before a
// build step.

const rfc3339DateTimePattern =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/

const cumulativeDaysBeforeMonth = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
const secondsPerDay = 86_400

function isLeapYear(year) {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
}

function daysBeforeYear(year) {
  const previousYear = year - 1
  return (
    previousYear * 365 +
    Math.floor(previousYear / 4) -
    Math.floor(previousYear / 100) +
    Math.floor(previousYear / 400)
  )
}

export function isRfc3339DateTime(value) {
  const match = rfc3339DateTimePattern.exec(value)
  if (match === null) {
    return false
  }

  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const hour = Number(match[4])
  const minute = Number(match[5])
  const second = Number(match[6])
  const offsetSign = match[7]
  const offsetHour = match[8] === undefined ? 0 : Number(match[8])
  const offsetMinute = match[9] === undefined ? 0 : Number(match[9])
  const maximumDay = [31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][
    month - 1
  ]

  const componentsAreValid =
    year >= 1 &&
    year <= 9999 &&
    month >= 1 &&
    month <= 12 &&
    maximumDay !== undefined &&
    day >= 1 &&
    day <= maximumDay &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    offsetHour <= 23 &&
    offsetMinute <= 59
  if (!componentsAreValid) {
    return false
  }

  const dayOfYear = cumulativeDaysBeforeMonth[month - 1]
  if (dayOfYear === undefined) {
    return false
  }
  const leapDay = month > 2 && isLeapYear(year) ? 1 : 0
  const localDay = daysBeforeYear(year) + dayOfYear + leapDay + day - 1
  const localSecond = localDay * secondsPerDay + hour * 3_600 + minute * 60 + second
  const offsetDirection = offsetSign === "-" ? -1 : 1
  const offsetSecond = offsetDirection * (offsetHour * 3_600 + offsetMinute * 60)
  const utcSecond = localSecond - offsetSecond
  const maximumUtcSecondExclusive = daysBeforeYear(10_000) * secondsPerDay

  return utcSecond >= 0 && utcSecond < maximumUtcSecondExclusive
}

export const consoleFormats = {
  "date-time": {type: "string", validate: isRfc3339DateTime},
}
