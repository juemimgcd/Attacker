import { useState } from "react";
import { Button, Modal, Typography } from "antd";
import { CodeOutlined } from "@ant-design/icons";

/** 折叠展示任意 JSON 证据，保持长内容不撑破布局。 */
export default function JsonViewer({ data, title }: { data: unknown; title?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        size="small"
        type="text"
        icon={<CodeOutlined />}
        onClick={() => setOpen(true)}
      >
        查看
      </Button>
      <Modal
        title={title ?? "详情"}
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={720}
      >
        <Typography.Paragraph>
          <pre className="json-view mono">
            {JSON.stringify(data, null, 2)}
          </pre>
        </Typography.Paragraph>
      </Modal>
    </>
  );
}
