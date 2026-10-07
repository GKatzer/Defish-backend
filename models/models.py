from sqlalchemy import Column, Integer, String, DateTime, Text, Float, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base
from pydantic import BaseModel
from typing import Optional, Union, List, Any


class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String(45), unique=True, index=True, nullable=False)  # IPv6 under 45 symbols
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    analyses = relationship("FishAnalysis", back_populates="user")

class FishAnalysis(Base):
    __tablename__ = "fish_analyses"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    
    # Analyzys data
    image_path = Column(String, nullable=False)  # original image path
    processed_image_path = Column(String)  # path to processed image
    total_objects = Column(Integer, default=0)  # count of detected objects
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    user = relationship("User", back_populates="analyses")
    detections = relationship("Detection", back_populates="analysis", cascade="all, delete-orphan")

class Detection(Base):
    __tablename__ = "detections"
    
    id = Column(Integer, primary_key=True, index=True)
    analysis_id = Column(Integer, ForeignKey("fish_analyses.id"))
    
    x_min = Column(Float, nullable=False)
    y_min = Column(Float, nullable=False)
    x_max = Column(Float, nullable=False)
    y_max = Column(Float, nullable=False)
    
    # Classes
    detection_class = Column(String(50), default="0")
    classification_class = Column(String(50))
    
    # Confidences
    detection_confidence = Column(Float)
    classification_confidence = Column(Float)
    
    # Results
    recommendations = Column(Text)
    
    # Relationships
    analysis = relationship("FishAnalysis", back_populates="detections")

class AnalysisResponse(BaseModel):
    id: str
    diagnosis: str
    confidence: float
    recommendations: str
    original_image: Optional[str] = None
    image_format: Optional[str] = "jpeg"
    image_width: int = 0
    image_height: int = 0
    detections: Optional[List[Any]] = None

class TaskResponse(BaseModel):
    task_id: str
    cached: bool
    message: Optional[str] = None
    result: Optional[AnalysisResponse] = None

class ProcessingResponse(BaseModel):
    status: str
    message: str
    task_id: str

PostResponses = Union[AnalysisResponse, TaskResponse]
AnalyzeResultResponse = Union[AnalysisResponse, ProcessingResponse]