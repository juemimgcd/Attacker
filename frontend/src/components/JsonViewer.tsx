import { useState } from "react";
import { Check, Code2, Copy } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";

export default function JsonViewer({
  data,
  title,
}: {
  data: unknown;
  title?: string;
}) {
  const [copied, setCopied] = useState(false);
  const json = JSON.stringify(data, null, 2) ?? "null";

  const copyJson = async () => {
    try {
      await navigator.clipboard.writeText(json);
      setCopied(true);
    } catch {
      toast.error("复制失败，请选择内容后手动复制。");
    }
  };

  return (
    <Dialog onOpenChange={() => setCopied(false)}>
      <DialogTrigger asChild>
        <Button size="sm" variant="ghost" aria-label={`查看${title ?? "详情"}`}>
          <Code2 data-icon="inline-start" />
          查看
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{title ?? "详情"}</DialogTitle>
          <DialogDescription>查看完整结构化数据，可复制用于进一步分析。</DialogDescription>
        </DialogHeader>
        <div className="json-panel">
          <div className="json-toolbar">
            <div className="flex items-center gap-2">
              <Badge variant="outline">JSON</Badge>
              <span>{json.split("\n").length} 行</span>
            </div>
            <Button size="sm" variant="ghost" onClick={copyJson} aria-live="polite">
              {copied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
              {copied ? "已复制" : "复制 JSON"}
            </Button>
          </div>
          <ScrollArea className="json-scroll">
            <pre className="json-view mono" tabIndex={0} aria-label="JSON 内容">{json}</pre>
          </ScrollArea>
        </div>
      </DialogContent>
    </Dialog>
  );
}
