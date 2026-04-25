import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";

type PlaceholderPageProps = Readonly<{
  title: string;
  description: string;
  planned?: string[];
  eyebrow?: string;
}>;

export function PlaceholderPage({
  title,
  description,
  planned = [],
  eyebrow = "planned",
}: PlaceholderPageProps) {
  return (
    <div className="space-y-4 pb-4">
      <Panel eyebrow={eyebrow} title={title} action={<StatusBadge tone="neutral">not live yet</StatusBadge>}>
        <div className="rounded-[1.2rem] border border-dashed border-border/70 bg-background/40 p-5">
          <p className="max-w-3xl text-sm leading-7 text-foreground/65">{description}</p>
          {planned.length > 0 ? (
            <div className="mt-5 grid gap-3 md:grid-cols-2">
              {planned.map((item) => (
                <div key={item} className="rounded-xl border border-border/60 bg-background/60 px-4 py-3 text-sm text-foreground/70">
                  {item}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </Panel>
    </div>
  );
}
