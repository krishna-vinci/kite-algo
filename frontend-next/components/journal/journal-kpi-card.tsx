import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type JournalKpiCardProps = {
  label: string;
  children: React.ReactNode;
  className?: string;
};

export function JournalKpiCard({ label, children, className }: JournalKpiCardProps) {
  return (
    <Card className={cn("gap-3 py-4", className)}>
      <CardHeader className="px-4 pb-0 pt-0">
        <CardTitle className="text-[11px] font-medium uppercase tracking-widest text-muted-foreground">
          {label}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4">{children}</CardContent>
    </Card>
  );
}
