# from ultralytics import YOLO

# model =YOLO("yolov8s-oiv7.pt")


# model.train(data="data.yaml", epochs=600, batch=8, imgsz=640, device=0,workers=0)



from ultralytics import YOLO

def main():
    model = YOLO("yolo11n.pt")

    model.train(
    data="data.yaml",
    epochs=600,
    batch=4,
    imgsz=640,
    workers=2,
    device=0,
    patience=50
)

if __name__ == "__main__":
    main()