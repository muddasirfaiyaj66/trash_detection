from ultralytics import YOLO
model =YOLO("best.pt")

model.predict(source="test.jpg",save=True,conf=0.4,classes=[1,2])
# line_width=2,save_crop=True,save_text=True,show_labels=True,show_conf=True,
# model.predict(source="test.jpg",show=True,conf=0.6,line_width=2,classes=[0,1,2])


# model.export(format="onnx")
