import { formatMoney, sourceOf } from "./format";
import type { Calculation, RequestStep, Source } from "./types";

// How an approval's payout was worked out, line by line.
export function CalculationTable({
  calculation,
}: Readonly<{ calculation: Calculation }>) {
  return (
    <div className="table-scroll calculation">
      <table className="table">
        <caption className="small muted">How the payout was worked out</caption>
        <tbody>
          {calculation.lines.map((line, index) => (
            <tr key={`${String(index)} ${line.step}`}>
              <td>{line.step}</td>
              <td className="num mono">{formatMoney(line.amount)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row">Payout</th>
            <td className="num mono">{formatMoney(calculation.payout)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

interface FileUsed {
  source: Source;
  lookedUp: Set<string>;
}

interface ContextFound {
  files: FileUsed[];
  foundNothing: Set<string>;
}

function contextFound(steps: RequestStep[]): ContextFound {
  const files = new Map<string, FileUsed>();
  const foundNothing = new Set<string>();
  for (const lookup of steps.filter((step) => step.stage === "lookup")) {
    const text = lookup.message;
    if (!lookup.sources || lookup.sources.length === 0) {
      foundNothing.add(text);
    } else {
      for (const source of lookup.sources) {
        const used = files.get(source.file) ?? { source, lookedUp: new Set() };
        used.lookedUp.add(text);
        files.set(source.file, used);
      }
    }
  }
  return { files: [...files.values()], foundNothing };
}

function LookedUpList({ lookedUp }: Readonly<{ lookedUp: Set<string> }>) {
  return (
    <ul>
      {[...lookedUp].map((looked) => (
        <li key={looked}>{looked}</li>
      ))}
    </ul>
  );
}

export function ContextUsed({ steps }: Readonly<{ steps?: RequestStep[] }>) {
  const { files, foundNothing } = contextFound(steps ?? []);
  if (files.length === 0 && foundNothing.size === 0) return null;
  return (
    <section
      className="panel context-used-panel"
      aria-labelledby="context-used-title"
    >
      <h2 id="context-used-title">Context used</h2>
      <div className="context-files">
        {files.map(({ source, lookedUp }) => (
          <div className="context-file" key={source.file}>
            <h3 className="mono">{source.file}</h3>
            <p className="small muted">{sourceOf(source)}</p>
            <LookedUpList lookedUp={lookedUp} />
          </div>
        ))}
        {foundNothing.size > 0 && (
          <div className="context-file">
            <h3>Found nothing</h3>
            <LookedUpList lookedUp={foundNothing} />
          </div>
        )}
      </div>
    </section>
  );
}
