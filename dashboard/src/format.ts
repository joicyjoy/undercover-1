import type { Source } from "./types";

const money = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});
const dateTime = new Intl.DateTimeFormat("en-IN", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Asia/Kolkata",
});
const day = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

export function humanize(value: string) {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function formatDate(value: string) {
  return dateTime.format(new Date(value));
}

export function date(isoDate: string): string {
  return day.format(new Date(`${isoDate}T00:00:00Z`));
}

export function sourceOf(source: Source): string {
  const owner = source.owner.startsWith("Head of ")
    ? `the ${source.owner}`
    : source.owner;
  return `${source.system}, owned by ${owner}, reviewed ${date(source.reviewed)}`;
}

export function formatMoney(value: number) {
  return money.format(value);
}
